"""
APScheduler AsyncIOScheduler wiring.

Two kinds of jobs run on a single AsyncIOScheduler:

* a global "heartbeat" job (``bus_flow``) that runs the default-config flow every
  ``SCHEDULER_INTERVAL`` minutes — kept for backwards compatibility and surfaced
  via the existing /scheduler/status fields (next_run / last_run / last_exit_code);
* one **per-reservation re-lock job** (``relock_<id>``) registered when a
  reservation first locks successfully. It re-locks the seat every
  ``SCHEDULER_INTERVAL`` minutes until the trip's departure datetime passes, then
  auto-expires.

All flows are blocking (Playwright sync API) so they are dispatched to a
thread-pool executor and serialised behind the global FLOW_LOCK — one browser at
a time.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..config import settings
from ..models.schemas import ReservationRequest, ReservationStatus
from ..services.flow import resolve_route_params, run_flow
from ..state import FLOW_LOCK, store
from ..utils.logger import log

_JOB_ID = "bus_flow"
_RELOCK_PREFIX = "relock_"


@dataclass
class JobState:
    last_run: Optional[datetime] = None
    last_exit_code: Optional[int] = None


job_state = JobState()
scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")


def _relock_job_id(reservation_id: str) -> str:
    return f"{_RELOCK_PREFIX}{reservation_id}"


# ─────────────────────────────────────────────────────────────────
# Global heartbeat job (kept, demoted)
# ─────────────────────────────────────────────────────────────────
async def _scheduled_flow() -> None:
    """Default-config heartbeat: run run_flow() off the event loop, serialised."""
    params = resolve_route_params(ReservationRequest())
    log.info("[scheduler] starting heartbeat flow")
    job_state.last_run = datetime.now(timezone.utc)
    try:
        loop = asyncio.get_running_loop()
        async with FLOW_LOCK:  # one browser/profile at a time
            code, _trip = await loop.run_in_executor(None, run_flow, params)
        job_state.last_exit_code = code
        log.info(f"[scheduler] heartbeat finished — exit={code}")
    except Exception as exc:
        job_state.last_exit_code = 2
        log.exception(f"[scheduler] heartbeat raised: {exc!r}")


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
        ReservationRequest(
            origin_id=record.origin_id,
            destination_id=record.destination_id,
            date=record.date,
            departure=record.departure,
            seat=record.seat,
        )
    )

    # (3) Run the flow off the loop, serialised behind FLOW_LOCK.
    try:
        loop = asyncio.get_running_loop()
        async with FLOW_LOCK:
            code, _trip = await loop.run_in_executor(None, run_flow, params)
    except Exception as exc:
        # (7) Unexpected error — log with traceback, keep the job.
        log.exception(f"scheduler: re-lock #{n} raised for {reservation_id}: {exc!r}")
        return

    if code == 0:
        # (4) Success — relock_count++ and back to locked.
        await store.update(
            reservation_id,
            status=ReservationStatus.locked,
            exit_code=0,
            error_msg=None,
            relock_count=n,
        )
        log.info(
            f"scheduler: re-lock #{n} OK — seat {record.seat} locked, "
            f"next run in {settings.scheduler_interval} min"
        )
    elif code == 1:
        # (5) Soft fail (seat unavailable) — keep retrying.
        await store.update(
            reservation_id,
            status=ReservationStatus.failed,
            exit_code=1,
            error_msg="seat unavailable or lock failed",
        )
        log.warning(f"scheduler: re-lock #{n} seat {record.seat} unavailable — will retry")
    else:
        # (6) Hard fail — stop retrying.
        await store.update(
            reservation_id,
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


# ─────────────────────────────────────────────────────────────────
# Lifecycle / status
# ─────────────────────────────────────────────────────────────────
def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        _scheduled_flow,
        trigger=IntervalTrigger(minutes=settings.scheduler_interval),
        id=_JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    log.info(f"[scheduler] started — every {settings.scheduler_interval} min")


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("[scheduler] shut down")


def next_run_time() -> Optional[datetime]:
    job = scheduler.get_job(_JOB_ID) if scheduler.running else None
    return job.next_run_time if job else None


def pause_global_job() -> Optional[datetime]:
    """Pause the global heartbeat job. Per-reservation re-locks keep running."""
    if scheduler.running:
        try:
            scheduler.pause_job(_JOB_ID)
            log.warning("[scheduler] global heartbeat paused")
        except JobLookupError:
            pass
    return next_run_time()  # None once paused


def resume_global_job() -> Optional[datetime]:
    """Resume the global heartbeat job."""
    if scheduler.running:
        try:
            scheduler.resume_job(_JOB_ID)
            log.info("[scheduler] global heartbeat resumed")
        except JobLookupError:
            pass
    return next_run_time()


def scheduler_status() -> dict:
    global_next = next_run_time()
    return {
        "running": scheduler.running,
        "interval_minutes": settings.scheduler_interval,
        "next_run": global_next,
        "last_run": job_state.last_run,
        "last_exit_code": job_state.last_exit_code,
        "global_next_run": global_next,
    }
