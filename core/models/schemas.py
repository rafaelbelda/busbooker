"""Pydantic v2 models for all API input/output and internal flow params."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field


class ReservationStatus(str, Enum):
    pending = "pending"
    locked = "locked"
    failed = "failed"
    cancelled = "cancelled"
    expired = "expired"  # departure datetime passed — re-lock cycle stopped


class ReservationRequest(BaseModel):
    """POST /reservations body — every field is required.

    There are no server-side route defaults: a reservation always describes a
    route the user explicitly chose. Omitting any field is a 422.
    """

    model_config = ConfigDict(extra="forbid")

    origin_id: str = Field(examples=["19058"])
    destination_id: str = Field(examples=["21787"])
    date: str = Field(examples=["2026-05-28"], description="yyyy-mm-dd")
    departure: str = Field(examples=["00:00"])
    seat: str = Field(examples=["00"])


class RouteParams(BaseModel):
    """Fully-resolved per-flow parameters (request merged with config defaults)."""

    model_config = ConfigDict(frozen=True)

    origin_id: str
    destination_id: str
    date: str
    departure: str
    seat: str
    date_formatted: str
    search_url: str


class ReservationRecord(BaseModel):
    """A single anonymous reservation attempt held in the in-memory store."""

    id: str
    origin_id: str
    destination_id: str
    date: str
    departure: str
    seat: str
    status: ReservationStatus = ReservationStatus.pending
    exit_code: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    error_msg: Optional[str] = None
    # Absolute UTC departure datetime; set once the first lock succeeds and the
    # trip resolves. Drives the scheduler's expiry decision.
    departure_datetime: Optional[datetime] = None
    # Incremented on each successful re-lock cycle.
    relock_count: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_expired(self) -> bool:
        """True once the bus has departed (departure_datetime is in the past)."""
        if self.departure_datetime is None:
            return False
        return datetime.now(timezone.utc) >= self.departure_datetime


class SeatInfo(BaseModel):
    number: str
    available: bool


class SeatsResponse(BaseModel):
    origin_id: str
    destination_id: str
    date: str
    departure: str
    total: int
    available: int
    seats: list[SeatInfo]


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    scheduler_running: bool


class RelockJobInfo(BaseModel):
    """Per-reservation re-lock job summary for /scheduler/status."""

    reservation_id: str
    next_run: Optional[datetime] = None
    relock_count: int = 0
    departure_datetime: Optional[datetime] = None
    minutes_until_departure: Optional[float] = None


class SchedulerStatusResponse(BaseModel):
    """Scheduler state. The scheduler only manages per-reservation re-lock jobs;
    there is no global/automatic booking job."""

    running: bool
    interval_minutes: int
    active_relock_count: int = 0
    active_relock_jobs: list[RelockJobInfo] = Field(default_factory=list)


class MessageResponse(BaseModel):
    detail: str


# ─────────────────────────────────────────────────────────────────
# URL-based search (POST /search)
# ─────────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        examples=[
            "https://mobifacil.com.br/passagem-de-onibus/sao-paulo-todos-sp/araraquara-sp"
            "?origin=-3&destination=19052&date=30-05-2026&isStudent=false&isPCD=false&searchValidDay=true"
        ]
    )


class TripSeat(BaseModel):
    """A seat as returned by /search (raw mobifacil seatMap field names).

    Distinct from ``SeatInfo`` (used by /seats), which keeps its existing
    ``number``/``available`` shape.
    """

    numero: str
    disponivel: bool
    posX: float = 0.0
    posY: float = 0.0


class TripResult(BaseModel):
    service_id: str
    departure: str
    arrival: str
    company: str
    price: str
    service_class: str
    seats: list[TripSeat] = Field(default_factory=list)


class SearchResponse(BaseModel):
    origin_id: str
    destination_id: str
    date: str
    trips: list[TripResult] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────
# Admin
# ─────────────────────────────────────────────────────────────────
class AdminStats(BaseModel):
    total: int
    pending: int
    locked: int
    failed: int
    cancelled: int
    expired: int
