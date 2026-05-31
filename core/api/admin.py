"""
Password-protected admin API (mounted at /admin).

All routes require HTTP Basic Auth — username ``admin`` and the password from the
ADMIN_PASSWORD env var (validated at startup in config.py). Credentials are
compared with ``secrets.compare_digest`` to avoid timing attacks.
"""
from __future__ import annotations

import os
import secrets
import signal
import time
from collections import Counter

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..config import settings
from ..models.schemas import AdminStats, ReservationRecord, ReservationStatus
from ..scheduler.jobs import cancel_relock, scheduler_status
from ..state import store
from ..utils.logger import log
from ..utils.net import client_info

_ADMIN_USER = "admin"
_security = HTTPBasic()


def _client(request: Request) -> str:
    """Client identity string for audit logs (reuses what the middleware stashed)."""
    info = getattr(request.state, "client", None) or client_info(request)
    return info.log_str()


def require_admin(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(_security),
) -> str:
    """Constant-time Basic Auth check. Raises 401 (+ WWW-Authenticate) on failure.

    Every admin access attempt is logged and attributable: failures at WARNING
    (so brute-force / probing is visible in the log), successes at INFO.
    """
    user_ok = secrets.compare_digest(credentials.username, _ADMIN_USER)
    pass_ok = secrets.compare_digest(credentials.password, settings.admin_password or "")
    if not (user_ok and pass_ok):
        log.warning(
            f"[admin] AUTH FAILURE user={credentials.username!r} "
            f"path={request.url.path} {_client(request)}"
        )
        raise HTTPException(
            status_code=401,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    log.info(f"[admin] access path={request.url.path} {_client(request)}")
    return credentials.username


# Router-level dependency → every /admin/* route is authenticated.
admin_router = APIRouter(dependencies=[Depends(require_admin)])


# ─────────────────────────────────────────────────────────────────
# Reservations
# ─────────────────────────────────────────────────────────────────
@admin_router.get("/reservations", response_model=list[ReservationRecord])
async def admin_list_reservations() -> list[ReservationRecord]:
    return await store.list()


@admin_router.get("/reservations/{reservation_id}", response_model=ReservationRecord)
async def admin_get_reservation(reservation_id: str) -> ReservationRecord:
    record = await store.get(reservation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="reservation not found")
    return record


@admin_router.delete("/reservations/{reservation_id}", response_model=ReservationRecord)
async def admin_force_cancel(reservation_id: str, request: Request) -> ReservationRecord:
    """Force-cancel any reservation regardless of status (keeps the record)."""
    record = await store.get(reservation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="reservation not found")
    cancel_relock(reservation_id)
    updated = await store.update(
        reservation_id,
        status=ReservationStatus.cancelled,
        error_msg="force-cancelled by admin",
    )
    log.warning(f"[audit] admin force-cancel reservation={reservation_id} {_client(request)}")
    return updated if updated is not None else record


# ─────────────────────────────────────────────────────────────────
# Scheduler status (read-only)
# ─────────────────────────────────────────────────────────────────
@admin_router.get("/scheduler")
async def admin_scheduler() -> dict:
    """Scheduler running state, interval, and active re-lock job count."""
    return scheduler_status()


# ─────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────
@admin_router.get("/stats", response_model=AdminStats)
async def admin_stats() -> AdminStats:
    records = await store.list()
    counts = Counter(r.status for r in records)
    return AdminStats(
        total=len(records),
        pending=counts.get(ReservationStatus.pending, 0),
        locked=counts.get(ReservationStatus.locked, 0),
        failed=counts.get(ReservationStatus.failed, 0),
        cancelled=counts.get(ReservationStatus.cancelled, 0),
        expired=counts.get(ReservationStatus.expired, 0),
    )


# ─────────────────────────────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────────────────────────────
def _delayed_sigterm() -> None:
    """Sleep briefly so the HTTP response flushes, then SIGTERM ourselves."""
    time.sleep(1)
    log.warning(f"admin shutdown: sending SIGTERM to pid {os.getpid()}")
    os.kill(os.getpid(), signal.SIGTERM)


@admin_router.post("/shutdown")
async def admin_shutdown(request: Request, background_tasks: BackgroundTasks):
    """Refuse if any reservation is active; otherwise SIGTERM self after 1s."""
    records = await store.list()
    active = [
        r for r in records
        if r.status in (ReservationStatus.pending, ReservationStatus.locked)
    ]
    if active:
        log.warning(
            f"[audit] admin shutdown REFUSED — {len(active)} active reservation(s) "
            f"{_client(request)}"
        )
        return JSONResponse(
            status_code=409,
            content={
                "error": "active_reservations",
                "count": len(active),
                "message": f"Cannot shut down: {len(active)} reservation(s) still active.",
            },
        )
    log.warning(f"[audit] admin shutdown triggered — no active reservations {_client(request)}")
    background_tasks.add_task(_delayed_sigterm)
    return {"status": "shutting_down"}
