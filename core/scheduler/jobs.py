"""
APScheduler AsyncIOScheduler wiring.

The scheduler manages exactly one kind of job: a **per-reservation re-lock job**
(``relock_<id>``) registered when a user-created reservation first locks
successfully. It re-locks that seat every ``SCHEDULER_INTERVAL`` minutes until
the trip's departure datetime passes, then auto-expires.

There is no global/heartbeat/default-route job. The scheduler never starts a
booking flow on its own — a flow only runs in response to an explicit user
reservation, or a re-lock belonging to one.

All flows are blocking (Playwright sync API) so they are dispatched to a
thread-pool executor and serialised behind the global FLOW_LOCK — one browser at
a time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..config import settings
from ..models.schemas import ReservationStatus
from ..services.flow import resolve_route_params, run_flow_guarded
from ..state import store
from ..utils.logger import log
from ..utils.time_utils import relock_cutoff

_RELOCK_PREFIX = "relock_"

# APScheduler defaults misfire_grace_time to 1 SECOND: a job whose fire time passes
# while the event loop is busy is silently DISCARDED ("Run time of job was missed")
# rather than run late. For a service whose whole purpose is firing reliably every N
# minutes for hours that is the wrong default — a slow SQLite commit, a burst of
# requests or a host suspend is enough to blow a 1 s budget, and the seat then stops
# being re-locked with no error anywhere. 5 minutes is still far inside the interval,
# so a late run is always better than a skipped one.
_MISFIRE_GRACE_SECONDS = 300

scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")


def _relock_job_id(reservation_id: str) -> str:
    return f"{_RELOCK_PREFIX}{reservation_id}"


# First retry delay after a soft fail, in minutes. Doubles per consecutive failure
# up to SCHEDULER_INTERVAL.
_SOFT_FAIL_BASE_MINUTES = 5


def _soft_fail_delay(consecutive_failures: int) -> int:
    """Minutes to wait before the next attempt after ``n`` failures in a row.

    5 → 10 → 20 … capped at ``SCHEDULER_INTERVAL``.

    The old code hard-coded 5 minutes and rescheduled to 5 again on *every*
    consecutive failure, so a seat taken by someone else produced a permanent
    5-minute flow loop — up to ~570 full browser flows across a 48-hour window,
    each launching Chromium and loading the provider's search page. That is the
    most likely way this service gets its IP banned, and it engaged exactly when
    things were already going wrong. Backing off returns to the normal cadence
    instead of hammering.
    """
    n = max(1, consecutive_failures)
    delay = _SOFT_FAIL_BASE_MINUTES * (2 ** (n - 1))
    return int(min(delay, settings.scheduler_interval))


# ─────────────────────────────────────────────────────────────────
# Per-reservation re-lock
# ─────────────────────────────────────────────────────────────────
async def _relock_job(reservation_id: str) -> None:
    """Repeating job: re-lock one reservation's seat until its departure passes."""
    record = await store.get(reservation_id)

    # (1) Gone or user-terminal — stop. NOTE: "failed" is intentionally NOT
    # treated as terminal here. An exit-1 soft fail sets status=failed but must
    # keep retrying (step 5), and an exit-2 hard fail removes its own job (step
    # 6) — so a live job never legitimately sees a hard-failed record. Treating
    # "failed" as terminal here would kill the retry loop after a single soft
    # fail, contradicting the "keep re-locking" requirement.
    if record is None or record.status in (ReservationStatus.cancelled, ReservationStatus.expired):
        cancel_relock(reservation_id)
        return

    # (2) Departure imminent or passed — expire and stop.
    #
    # The cutoff sits BEFORE departure, not at it. mobifacil delists a trip some
    # minutes before it leaves, and a re-lock that lands after delisting cannot
    # succeed: it used to burn a full flow, a profile reset and a whole retry to
    # discover that (observed firing at T-89s for a trip already gone). Re-locking
    # during boarding buys nothing anyway.
    now = datetime.now(timezone.utc)
    if record.departure_datetime is not None:
        cutoff = relock_cutoff(
            record.departure_datetime, settings.relock_stop_minutes_before_departure
        )
        if now >= cutoff:
            await store.update(reservation_id, status=ReservationStatus.expired)
            log.info(
                f"scheduler: reservation {reservation_id} expired "
                f"(departure {record.departure_datetime}, stopping "
                f"{settings.relock_stop_minutes_before_departure} min before) — job removed"
            )
            cancel_relock(reservation_id)
            return

    n = record.relock_count + 1
    log.info(
        f"scheduler: starting re-lock #{n} for reservation {reservation_id} "
        f"(seat {record.seat}, {record.date} {record.departure})"
    )
    params = resolve_route_params(
        origin_id=record.origin_id,
        destination_id=record.destination_id,
        date=record.date,
        departure=record.departure,
        seat=record.seat,
    )

    # (3) Run the flow — serialised, time-bounded and observable.
    try:
        code, _trip = await run_flow_guarded(params, reservation_id, is_relock=True)
    except Exception as exc:
        # (7) Unexpected error — log with traceback, keep the job.
        log.exception(f"scheduler: re-lock #{n} raised for {reservation_id}: {exc!r}")
        return

    if code == 0:
        # (4) Success — relock_count++ and back to locked.
        updated = await store.update(
            reservation_id,
            only_if_active=True,
            status=ReservationStatus.locked,
            exit_code=0,
            error_msg=None,
            relock_count=n,
            consecutive_failures=0,   # success resets the backoff ladder
        )
        if updated is None:
            # Cancelled/expired/deleted while this re-lock ran — don't resurrect.
            log.info(
                f"scheduler: re-lock #{n} for {reservation_id} completed but record is "
                "terminal/gone — outcome discarded, job removed"
            )
            cancel_relock(reservation_id)
            return
        log.info(
            f"scheduler: re-lock #{n} OK — seat {record.seat} locked, "
            f"next run in {settings.scheduler_interval} min"
        )
    elif code == 1:
        # (5) Soft fail (seat unavailable) — keep retrying.
        updated = await store.update(
            reservation_id,
            only_if_active=True,
            status=ReservationStatus.failed,
            exit_code=1,
            error_msg="seat unavailable or lock failed",
            consecutive_failures=record.consecutive_failures + 1,
        )
        if updated is None:
            log.info(
                f"scheduler: re-lock #{n} soft-fail but {reservation_id} terminal/gone "
                "— job removed"
            )
            cancel_relock(reservation_id)
            return
        retry_in = _soft_fail_delay(updated.consecutive_failures)
        try:
            scheduler.reschedule_job(
                _relock_job_id(reservation_id),
                # NOTE: do NOT pass misfire_grace_time here. reschedule_job forwards
                # extra kwargs to the *trigger* constructor, and since we hand it a
                # trigger instance they are silently discarded — it would read as
                # configured while doing nothing. modify_job preserves the value set
                # by add_job, so the job keeps _MISFIRE_GRACE_SECONDS across this.
                trigger=IntervalTrigger(
                    minutes=settings.scheduler_interval,
                    start_date=datetime.now(timezone.utc) + timedelta(minutes=retry_in),
                ),
            )
            log.warning(
                f"scheduler: re-lock #{n} seat {record.seat} soft-fail "
                f"(#{updated.consecutive_failures} in a row) — retrying in {retry_in} min, "
                f"then every {settings.scheduler_interval} min"
            )
        except JobLookupError:
            log.warning(f"scheduler: re-lock #{n} seat {record.seat} soft-fail — job gone, cannot reschedule")
    elif code == 3:
        # Trip no longer offered — terminal, and not our fault. Expire rather than
        # fail: there is nothing to retry, the provider has stopped selling it.
        await store.update(
            reservation_id,
            only_if_active=True,
            status=ReservationStatus.expired,
            exit_code=3,
            error_msg="trip is no longer offered by the provider",
        )
        log.info(
            f"scheduler: re-lock #{n} — trip no longer offered for {reservation_id}; "
            "expiring and removing job"
        )
        cancel_relock(reservation_id)
    else:
        # (6) Hard fail — stop retrying.
        await store.update(
            reservation_id,
            only_if_active=True,
            status=ReservationStatus.failed,
            exit_code=2,
            error_msg="unrecoverable flow error",
        )
        log.error(f"scheduler: re-lock #{n} unrecoverable error — job removed for {reservation_id}")
        cancel_relock(reservation_id)


def schedule_relock(reservation_id: str, departure_dt: Optional[datetime] = None) -> None:
    """Register a repeating re-lock job for this reservation (first run in 1 interval).

    Skipped entirely when the pre-departure cutoff has already passed — the job's
    first run would do nothing but expire the record.
    """
    if departure_dt is not None:
        cutoff = relock_cutoff(departure_dt, settings.relock_stop_minutes_before_departure)
        if datetime.now(timezone.utc) >= cutoff:
            log.info(
                f"scheduler: not registering re-lock for {reservation_id} — within "
                f"{settings.relock_stop_minutes_before_departure} min of departure {departure_dt}"
            )
            return
    scheduler.add_job(
        _relock_job,
        trigger=IntervalTrigger(minutes=settings.scheduler_interval),
        id=_relock_job_id(reservation_id),
        args=[reservation_id],
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        misfire_grace_time=_MISFIRE_GRACE_SECONDS,
    )
    log.info(
        f"scheduler: registered re-lock job for {reservation_id} "
        f"(every {settings.scheduler_interval} min)"
    )


def cancel_relock(reservation_id: str) -> None:
    """Remove the re-lock job for this reservation if it exists."""
    try:
        scheduler.remove_job(_relock_job_id(reservation_id))
        log.info(f"scheduler: removed re-lock job for {reservation_id}")
    except JobLookupError:
        pass


def list_relock_jobs() -> list[tuple[str, Optional[datetime]]]:
    """Return (reservation_id, next_run) for every active re-lock job."""
    if not scheduler.running:
        return []
    out: list[tuple[str, Optional[datetime]]] = []
    for job in scheduler.get_jobs():
        if job.id.startswith(_RELOCK_PREFIX):
            out.append((job.id[len(_RELOCK_PREFIX):], job.next_run_time))
    return out


async def rehydrate_relocks() -> None:
    """Re-arm re-lock jobs for reservations restored from disk on startup.

    Only reservations that actually had a live re-lock cycle are rescheduled: a
    record is a candidate iff it is ``locked``/``failed`` (soft-fail keeps
    retrying) *and* has a future ``departure_datetime`` (set only once a seat
    first locked). Records that never locked, or whose departure already passed,
    are skipped — ``store.load`` has already expired the latter.
    """
    now = datetime.now(timezone.utc)
    count = 0
    for rec in await store.list():
        if (
            rec.status in (ReservationStatus.locked, ReservationStatus.failed)
            # `failed` covers two very different things. Exit 1 is a soft fail that
            # should keep retrying; exit 2 is unrecoverable and _relock_job
            # deliberately removed its own job. Re-arming the latter on restart put
            # known-broken reservations back to hammering the provider.
            and rec.exit_code != 2
            and rec.departure_datetime is not None
            and now < relock_cutoff(
                rec.departure_datetime, settings.relock_stop_minutes_before_departure
            )
        ):
            schedule_relock(rec.id, rec.departure_datetime)
            count += 1
    if count:
        log.info(f"scheduler: rehydrated {count} re-lock job(s) from persisted reservations")


# ─────────────────────────────────────────────────────────────────
# Lifecycle / status
# ─────────────────────────────────────────────────────────────────
def start_scheduler() -> None:
    # Starts the scheduler only. No booking job is registered at startup — flows
    # run solely from a user reservation or its own re-lock job.
    if scheduler.running:
        return
    scheduler.start()
    log.info("[scheduler] started (no startup booking job)")

def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("[scheduler] shut down")

def scheduler_status() -> dict:
    return {
        "running": scheduler.running,
        "interval_minutes": settings.scheduler_interval,
        "active_relock_count": len(list_relock_jobs()),
    }
