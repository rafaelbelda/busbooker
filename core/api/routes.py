"""HTTP endpoints for the bus-reserver service."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Response

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
from ..utils.logger import log
from ..utils.time_utils import compute_departure_datetime

router = APIRouter()

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
    params = resolve_route_params(
        origin_id=origin_id,
        destination_id=destination_id,
        date=date,
        departure=departure,
    )
    loop = asyncio.get_running_loop()
    try:
        async with FLOW_LOCK:  # browser flow — serialise with reservations
            seats = await loop.run_in_executor(None, fetch_seat_map, params)
    except Exception as exc:
        log.exception(f"[/seats] failed: {exc!r}")
        raise HTTPException(status_code=500, detail=f"seat map fetch failed: {exc}") from exc

    return SeatsResponse(
        origin_id=params.origin_id,
        destination_id=params.destination_id,
        date=params.date,
        departure=params.departure,
        total=len(seats),
        available=sum(1 for s in seats if s.available),
        seats=seats,
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
        async with FLOW_LOCK:  # browser flow — serialise with reservations
            trips_raw = await loop.run_in_executor(None, search_trips, params)
    except Exception as exc:
        log.exception(f"[/search] failed: {exc!r}")
        raise HTTPException(status_code=500, detail=f"search failed: {exc}") from exc

    return SearchResponse(
        origin_id=parsed["origin"],
        destination_id=parsed["destination"],
        date=parsed["date"],
        trips=[TripResult(**t) for t in trips_raw],
    )


# ─────────────────────────────────────────────────────────────────
# Reservations
# ─────────────────────────────────────────────────────────────────
@router.post("/reservations", response_model=ReservationRecord, status_code=201)
async def create_reservation(req: ReservationRequest, response: Response) -> ReservationRecord:
    params = resolve_route_params(
        origin_id=req.origin_id,
        destination_id=req.destination_id,
        date=req.date,
        departure=req.departure,
        seat=req.seat,
    )
    now = datetime.now(timezone.utc)
    record = ReservationRecord(
        id=str(uuid4()),
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

    loop = asyncio.get_running_loop()
    try:
        async with FLOW_LOCK:  # run_flow is blocking sync Playwright → executor
            code, trip = await loop.run_in_executor(None, run_flow, params)
    except Exception as exc:
        log.exception(f"[/reservations] flow raised: {exc!r}")
        response.status_code = 500
        return await _finalise(record.id, 2, error_override=str(exc))

    # On a successful first lock, record the absolute departure datetime and
    # register the per-reservation re-lock cycle.
    departure_dt = None
    if code == 0 and trip is not None:
        departure_dt = compute_departure_datetime(params.date, params.departure)

    response.status_code = _EXIT_HTTP.get(code, 500)
    updated = await _finalise(record.id, code, departure_dt=departure_dt)
    if code == 0:
        schedule_relock(record.id)
    return updated


async def _finalise(
    record_id: str,
    code: int,
    error_override: Optional[str] = None,
    departure_dt: Optional[datetime] = None,
) -> ReservationRecord:
    status = _EXIT_STATUS.get(code, ReservationStatus.failed)
    error = error_override if error_override is not None else _EXIT_ERR.get(code)
    fields: dict[str, object] = {"status": status, "exit_code": code, "error_msg": error}
    if departure_dt is not None:
        fields["departure_datetime"] = departure_dt
    updated = await store.update(record_id, **fields)
    if updated is None:  # should never happen — record was just created
        raise HTTPException(status_code=500, detail="reservation vanished mid-flow")
    return updated


@router.get("/reservations/{reservation_id}", response_model=ReservationRecord)
async def get_reservation(reservation_id: str) -> ReservationRecord:
    record = await store.get(reservation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="reservation not found")
    return record


@router.delete("/reservations/{reservation_id}", response_model=MessageResponse)
async def delete_reservation(reservation_id: str) -> MessageResponse:
    # Stop the re-lock job first so it cannot fire on a deleted id.
    cancel_relock(reservation_id)
    if not await store.delete(reservation_id):
        raise HTTPException(status_code=404, detail="reservation not found")
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
