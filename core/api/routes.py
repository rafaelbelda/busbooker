"""HTTP endpoints for the bus-reserver service."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ..models.schemas import (
    HealthResponse,
    MessageResponse,
    RelockJobInfo,
    ReservationRecord,
    ReservationRequest,
    ReservationStatus,
    SchedulerStatusResponse,
    SearchRequest,
    SearchResponse,
    SeatsResponse,
    TripResult,
    validate_date,
    validate_departure,
    validate_nonempty,
)
from ..scheduler.jobs import (
    cancel_relock,
    list_relock_jobs,
    schedule_relock,
    scheduler_status,
)
from ..services.flow import (
    fetch_seat_map,
    resolve_route_params,
    resolve_search_params,
    run_flow,
    search_trips,
)
from ..state import FLOW_LOCK, store, uptime_seconds
from ..utils.logger import log, read_reservation_log
from ..utils.net import client_info
from ..utils.ratelimit import browser_guard
from ..utils.time_utils import compute_arrival_datetime, compute_departure_datetime

router = APIRouter()


def _client(request: Request) -> str:
    """Client identity string for audit logs (reuses what the middleware stashed)."""
    info = getattr(request.state, "client", None) or client_info(request)
    return info.log_str()

# exit code → (reservation status, HTTP status, error message)
_EXIT_STATUS = {0: ReservationStatus.locked, 1: ReservationStatus.failed, 2: ReservationStatus.failed}
_EXIT_HTTP = {0: 201, 1: 409, 2: 500}
_EXIT_ERR = {1: "seat unavailable or lock failed", 2: "unrecoverable flow error"}


# ─────────────────────────────────────────────────────────────────
# Liveness
# ─────────────────────────────────────────────────────────────────
@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        uptime_seconds=round(uptime_seconds(), 2),
        scheduler_running=scheduler_status()["running"],
    )


# ─────────────────────────────────────────────────────────────────
# Live seat map
# ─────────────────────────────────────────────────────────────────
@router.get("/seats", response_model=SeatsResponse)
async def get_seats(
    origin_id: str = Query(),
    destination_id: str = Query(),
    date: str = Query(description="yyyy-mm-dd"),
    departure: str = Query(description="HH:MM departure to match"),
) -> SeatsResponse:
    # All route values are required query params — there are no server defaults.
    # Seat-map reads are not seat-specific, so no seat is needed here.
    try:
        origin_id = validate_nonempty(origin_id, "origin_id")
        destination_id = validate_nonempty(destination_id, "destination_id")
        date = validate_date(date)
        departure = validate_departure(departure)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    params = resolve_route_params(
        origin_id=origin_id,
        destination_id=destination_id,
        date=date,
        departure=departure,
    )
    loop = asyncio.get_running_loop()
    try:
        # No FLOW_LOCK: fetch_seat_map uses httpx, not the browser session.
        seats, decks = await loop.run_in_executor(None, fetch_seat_map, params)
    except RuntimeError as exc:
        if "no more trips for this date" in str(exc):
            log.info(f"[/seats] {exc}")
            raise HTTPException(status_code=404, detail="no more trips for this date") from exc
        log.exception(f"[/seats] failed: {exc!r}")
        raise HTTPException(status_code=500, detail="seat map fetch failed") from exc
    except Exception as exc:
        log.exception(f"[/seats] failed: {exc!r}")
        raise HTTPException(status_code=500, detail="seat map fetch failed") from exc

    return SeatsResponse(
        origin_id=params.origin_id,
        destination_id=params.destination_id,
        date=params.date,
        departure=params.departure,
        total=len(seats),
        available=sum(1 for s in seats if s.available),
        seats=seats,
        decks=decks,
    )


# ─────────────────────────────────────────────────────────────────
# URL-based search
# ─────────────────────────────────────────────────────────────────
def _parse_search_url(url: str) -> dict:
    """Validate a mobifacil passagem-de-onibus URL and extract route params.

    Raises HTTP 422 for anything that isn't a valid mobifacil search URL.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":")[0]
    if not (host == "mobifacil.com.br" or host.endswith(".mobifacil.com.br")):
        raise HTTPException(status_code=422, detail="URL must be a mobifacil.com.br link")
    if "passagem-de-onibus" not in parsed.path:
        raise HTTPException(status_code=422, detail="URL must be a passagem-de-onibus search URL")

    qs = parse_qs(parsed.query)
    origin = (qs.get("origin") or [None])[0]
    destination = (qs.get("destination") or [None])[0]
    date_raw = (qs.get("date") or [None])[0]
    if not origin or not destination or not date_raw:
        raise HTTPException(status_code=422, detail="URL missing origin/destination/date")

    # strptime validates it's a real dd-mm-yyyy date (rejects e.g. yyyy-mm-dd,
    # which a naive 3-numeric-parts check would misparse) and converts it.
    try:
        parsed_date = datetime.strptime(date_raw, "%d-%m-%Y")
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"date must be dd-mm-yyyy, got '{date_raw}'"
        ) from exc
    return {
        "origin": origin,
        "destination": destination,
        "date": parsed_date.strftime("%Y-%m-%d"),  # dd-mm-yyyy -> yyyy-mm-dd
        "is_student": (qs.get("isStudent") or ["false"])[0],
        "is_pcd": (qs.get("isPCD") or ["false"])[0],
    }


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    parsed = _parse_search_url(req.url)
    params = resolve_search_params(parsed["origin"], parsed["destination"], parsed["date"], req.url)
    loop = asyncio.get_running_loop()
    try:
        # No FLOW_LOCK: search_trips uses httpx, not the browser session.
        trips_raw = await loop.run_in_executor(None, search_trips, params)
    except Exception as exc:
        log.exception(f"[/search] failed: {exc!r}")
        raise HTTPException(status_code=500, detail="search failed") from exc

    return SearchResponse(
        origin_id=parsed["origin"],
        destination_id=parsed["destination"],
        date=parsed["date"],
        trips=[TripResult(**t) for t in trips_raw],
    )


# ─────────────────────────────────────────────────────────────────
# Reservations
# ─────────────────────────────────────────────────────────────────
@router.post(
    "/reservations",
    response_model=ReservationRecord,
    status_code=201,
    dependencies=[Depends(browser_guard)],
)
async def create_reservation(
    req: ReservationRequest, request: Request, response: Response
) -> ReservationRecord:
    params = resolve_route_params(
        origin_id=req.origin_id,
        destination_id=req.destination_id,
        date=req.date,
        departure=req.departure,
        seat=req.seat,
    )
    now = datetime.now(timezone.utc)
    dep_dt_check = compute_departure_datetime(req.date, req.departure)
    hours_ahead = (dep_dt_check - now).total_seconds() / 3600
    if hours_ahead < 0:
        raise HTTPException(status_code=422, detail="departure is in the past")
    if hours_ahead > 48:
        raise HTTPException(status_code=422, detail="departure is more than 48 hours in the future")
    record = ReservationRecord(
        id=req.id or str(uuid4()).split("-")[0],  # client-supplied or server-generated
        origin_id=params.origin_id,
        destination_id=params.destination_id,
        date=params.date,
        departure=params.departure,
        seat=params.seat,
        status=ReservationStatus.pending,
        created_at=now,
        updated_at=now,
    )
    await store.add(record)
    log.info(
        f"[audit] reservation create id={record.id} "
        f"route={params.origin_id}->{params.destination_id} date={params.date} "
        f"departure={params.departure} seat={params.seat} {_client(request)}"
    )

    loop = asyncio.get_running_loop()
    try:
        async with FLOW_LOCK:  # run_flow is blocking sync Playwright → executor
            code, trip = await loop.run_in_executor(None, run_flow, params, record.id)
    except Exception as exc:
        # Full detail to the log; the client gets a generic message (no internals).
        log.exception(f"[/reservations] id={record.id} flow raised: {exc!r}")
        response.status_code = 500
        updated = await _finalise(record.id, 2)
        return await _terminal_or(record.id, updated, response)

    # On a successful first lock, record the absolute departure and arrival datetimes
    # and register the per-reservation re-lock cycle.
    departure_dt = None
    arrival_dt = None
    if code == 0 and trip is not None:
        departure_dt = compute_departure_datetime(params.date, params.departure)
        arr_hhmm = trip.get("arrivalHour", "")
        if arr_hhmm:
            try:
                arrival_dt = compute_arrival_datetime(params.date, params.departure, arr_hhmm)
            except Exception as exc:
                log.warning(f"[/reservations] arrival_datetime skipped: {exc!r}")

    response.status_code = _EXIT_HTTP.get(code, 500)
    updated = await _finalise(record.id, code, departure_dt=departure_dt, arrival_dt=arrival_dt)
    if updated is None:
        # Reservation was cancelled/deleted while the flow ran — never resurrect
        # it or schedule a re-lock for a seat the user no longer wants.
        return await _terminal_or(record.id, updated, response)

    if code == 0:
        schedule_relock(record.id)
        log.info(f"[audit] reservation locked id={record.id} — re-lock scheduled {_client(request)}")
    else:
        log.info(
            f"[audit] reservation outcome id={record.id} "
            f"status={updated.status.value} exit={code} {_client(request)}"
        )
    return updated


async def _finalise(
    record_id: str,
    code: int,
    departure_dt: Optional[datetime] = None,
    arrival_dt: Optional[datetime] = None,
) -> Optional[ReservationRecord]:
    """Apply the flow outcome — but never revert a reservation that became
    terminal (cancelled/expired) or was deleted while the flow ran.

    Returns the updated record, or ``None`` if it is gone / already terminal.
    """
    status = _EXIT_STATUS.get(code, ReservationStatus.failed)
    fields: dict[str, object] = {
        "status": status,
        "exit_code": code,
        "error_msg": _EXIT_ERR.get(code),
    }
    if departure_dt is not None:
        fields["departure_datetime"] = departure_dt
    if arrival_dt is not None:
        fields["arrival_datetime"] = arrival_dt
    return await store.update(record_id, only_if_active=True, **fields)


async def _terminal_or(
    record_id: str, updated: Optional[ReservationRecord], response: Response
) -> ReservationRecord:
    """Handle the race where a record went terminal/gone during the flow."""
    if updated is not None:
        return updated
    response.status_code = 409
    current = await store.get(record_id)
    if current is None:
        log.warning(f"[audit] reservation {record_id} deleted during flow — outcome discarded")
        raise HTTPException(status_code=409, detail="reservation was cancelled during processing")
    log.warning(f"[audit] reservation {record_id} cancelled during flow — outcome discarded")
    return current


@router.get("/reservations/{reservation_id}", response_model=ReservationRecord)
async def get_reservation(reservation_id: str) -> ReservationRecord:
    record = await store.get(reservation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="reservation not found")
    return record


@router.get("/reservations/{reservation_id}/log")
async def get_reservation_log(
    reservation_id: str,
    tail_kb: int = Query(default=256, ge=1, le=4096, description="return at most the last N KB"),
) -> dict:
    """Public per-reservation flow log, scoped to a SINGLE id.

    The reservation id is the capability — exactly like ``GET /reservations/{id}``,
    the holder of an id can read that reservation's log and no other. (Admins use
    ``/admin/reservations/{id}/log`` to read any.) The log contains only flow steps
    and payloads — no client IPs (those are logged outside the flow context).
    404 if the id is unknown; ``exists:false`` while the flow hasn't produced a log.
    """
    if await store.get(reservation_id) is None:
        raise HTTPException(status_code=404, detail="reservation not found")
    data = read_reservation_log(reservation_id, tail_kb)
    if data is None:
        raise HTTPException(status_code=404, detail="reservation not found")
    return data


@router.delete("/reservations/{reservation_id}", response_model=MessageResponse)
async def delete_reservation(reservation_id: str, request: Request) -> MessageResponse:
    # Stop the re-lock job first so it cannot fire on a deleted id.
    cancel_relock(reservation_id)
    if not await store.delete(reservation_id):
        raise HTTPException(status_code=404, detail="reservation not found")
    log.info(f"[audit] reservation delete id={reservation_id} {_client(request)}")
    return MessageResponse(detail=f"reservation {reservation_id} cancelled")


# ─────────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────────
@router.get("/scheduler/status", response_model=SchedulerStatusResponse)
async def get_scheduler_status() -> SchedulerStatusResponse:
    base = scheduler_status()
    now = datetime.now(timezone.utc)
    relock_jobs: list[RelockJobInfo] = []
    for reservation_id, job_next_run in list_relock_jobs():
        record = await store.get(reservation_id)
        dep_dt = record.departure_datetime if record else None
        minutes = round((dep_dt - now).total_seconds() / 60, 1) if dep_dt else None
        relock_jobs.append(
            RelockJobInfo(
                reservation_id=reservation_id,
                next_run=job_next_run,
                relock_count=record.relock_count if record else 0,
                departure_datetime=dep_dt,
                minutes_until_departure=minutes,
            )
        )
    return SchedulerStatusResponse(**base, active_relock_jobs=relock_jobs)
