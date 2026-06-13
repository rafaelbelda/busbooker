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

import asyncio
from datetime import datetime, timezone
from typing import Optional

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..config import settings
from ..models.schemas import ReservationStatus
from ..services.flow import resolve_route_params, run_flow
from ..state import FLOW_LOCK, store
from ..utils.logger import log

_RELOCK_PREFIX = "relock_"

scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")


def _relock_job_id(reservation_id: str) -> str:
    return f"{_RELOCK_PREFIX}{reservation_id}"


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

    # (2) Departure passed — expire and stop.
    now = datetime.now(timezone.utc)
    if record.departure_datetime is not None and now >= record.departure_datetime:
        await store.update(reservation_id, status=ReservationStatus.expired)
        log.info(
            f"scheduler: reservation {reservation_id} expired "
            f"(departure {record.departure_datetime}) — job removed"
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

    # (3) Run the flow off the loop, serialised behind FLOW_LOCK.
    try:
        loop = asyncio.get_running_loop()
        async with FLOW_LOCK:
            code, _trip = await loop.run_in_executor(None, run_flow, params, reservation_id)
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
        )
        if updated is None:
            log.info(
                f"scheduler: re-lock #{n} soft-fail but {reservation_id} terminal/gone "
                "— job removed"
            )
            cancel_relock(reservation_id)
            return
        log.warning(f"scheduler: re-lock #{n} seat {record.seat} unavailable — will retry")
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


def schedule_relock(reservation_id: str) -> None:
    """Register a repeating re-lock job for this reservation (first run in 1 interval)."""
    scheduler.add_job(
        _relock_job,
        trigger=IntervalTrigger(minutes=settings.scheduler_interval),
        id=_relock_job_id(reservation_id),
        args=[reservation_id],
        max_instances=1,
        coalesce=True,
        replace_existing=True,
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
            and rec.departure_datetime is not None
            and now < rec.departure_datetime
        ):
            schedule_relock(rec.id)
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
