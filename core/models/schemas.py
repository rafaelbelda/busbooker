"""Pydantic v2 models for all API input/output and internal flow params."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

# Canonical shape of a reservation id. Clients may supply their own so they can open
# the monitor before the flow finishes, which makes this an untrusted input that ends
# up in a FILENAME (``<date>-<id>.log``) — so it must not contain path separators,
# ``..`` or anything else that could escape the log directory. Defined here and reused
# by utils.logger so the API boundary and the filesystem boundary cannot drift apart.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ─────────────────────────────────────────────────────────────────
# Shared route-field validators (used by ReservationRequest and /seats)
# ─────────────────────────────────────────────────────────────────
def validate_nonempty(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    return value


def validate_date(value: str) -> str:
    """Require a real ``yyyy-mm-dd`` date (rejects garbage that would otherwise
    be sliced into a malformed search URL)."""
    value = value.strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date must be a valid yyyy-mm-dd date") from exc
    return value


def validate_departure(value: str) -> str:
    """Require a 24h ``HH:MM`` time."""
    value = value.strip()
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ValueError("departure must be HH:MM (24-hour)") from exc
    return value


class ReservationStatus(str, Enum):
    pending = "pending"
    locked = "locked"
    failed = "failed"
    cancelled = "cancelled"
    expired = "expired"  # departure datetime passed — re-lock cycle stopped


class ReservationRequest(BaseModel):
    """POST /reservations body.

    ``id`` is optional: the client may supply a pre-generated 8-char hex ID so
    it can navigate to the monitor immediately without waiting for the flow to
    complete.  When omitted the server generates one.
    """

    model_config = ConfigDict(extra="forbid")

    id: Optional[str] = Field(default=None, description="client-generated 8-char hex ID (optional)")
    origin_id: str = Field(examples=["19058"])
    destination_id: str = Field(examples=["21787"])
    date: str = Field(examples=["2026-05-28"], description="yyyy-mm-dd")
    departure: str = Field(examples=["00:00"], description="HH:MM (24-hour)")
    seat: str = Field(examples=["00"])

    @field_validator("id")
    @classmethod
    def _safe_id(cls, v: Optional[str]) -> Optional[str]:
        """Reject ids that are unsafe as a filename component.

        The id reaches the filesystem as ``<date>-<id>.log``, so an unvalidated value
        containing ``../`` would let a caller steer that write outside the log
        directory. Rejecting at the API boundary is the fix; utils.logger re-checks
        with the same pattern as defence in depth.
        """
        if v is None:
            return None
        v = v.strip()
        if not SAFE_ID_RE.match(v):
            raise ValueError(
                "id must be 1-64 characters of letters, digits, hyphen or underscore"
            )
        return v

    @field_validator("origin_id", "destination_id", "seat")
    @classmethod
    def _nonempty(cls, v: str, info) -> str:
        return validate_nonempty(v, info.field_name)

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        return validate_date(v)

    @field_validator("departure")
    @classmethod
    def _departure(cls, v: str) -> str:
        return validate_departure(v)


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
    # Absolute UTC arrival datetime; set on first lock when arrivalHour is available.
    arrival_datetime: Optional[datetime] = None
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
    posX: float = 0.0
    posY: float = 0.0
    posZ: float = 0.0   # floor index: 0 = ground, 1 = upper (double-decker)


class SeatCell(BaseModel):
    """One cell of the bus grid, mirroring mobifacil's raw seatMap structure so the
    frontend can render the coach exactly the way mobifacil does (no guessing).

    ``kind``:
      - ``seat``     — a bookable seat (use ``number``/``available``/``idoso``)
      - ``aisle``    — corridor / empty spacer (numero -99 or the central column); no label
      - ``marker``   — a labelled non-seat landmark (ES = stairs, GE); show ``number``, not clickable
      - ``bathroom`` — WC
    """

    kind: str
    number: str = ""          # raw label as mobifacil shows it ("05", "WC", "ES", "GE"); "" for aisle
    available: bool = False
    idoso: bool = False       # priority seat — reservable only via attendance


class SeatDeck(BaseModel):
    """A single deck/floor. ``rows`` are mobifacil's depth-slices (front→back); each
    row is a fixed-width list of cross-section cells (window→aisle→window)."""

    label: str
    rows: list[list[SeatCell]]


class SeatsResponse(BaseModel):
    origin_id: str
    destination_id: str
    date: str
    departure: str
    total: int
    available: int
    seats: list[SeatInfo]
    # Full grid mirroring mobifacil's render (decks → rows → cells). Lets the
    # frontend draw corridors, landmarks and floors exactly; ``seats`` above stays
    # the flat list used for counts. Empty when the provider returns no seatMap.
    decks: list[SeatDeck] = Field(default_factory=list)


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
    posZ: float = 0.0


class TripResult(BaseModel):
    service_id: str
    departure: str
    arrival: str
    departure_date: str = ""
    company: str
    price: str
    service_class: str
    duration: str = ""
    available_seats: int = 0
    has_second_floor: bool = False
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
