"""
In-memory, database-free application state.

Holds the anonymous reservation store (guarded by an ``asyncio.Lock``) plus the
global flow lock that serialises browser runs — the persistent Playwright
profile must never be driven by two flows at once.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

from .models.schemas import ReservationRecord, ReservationStatus

# Monotonic-ish process start, used for the /health uptime figure.
START_WALL: datetime = datetime.now(timezone.utc)
START_MONOTONIC: float = time.monotonic()

# Serialises every browser flow (single shared persistent profile / single worker).
FLOW_LOCK: asyncio.Lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ReservationStore:
    """UUID-keyed reservation store protected by a single asyncio lock."""

    def __init__(self) -> None:
        self._items: dict[str, ReservationRecord] = {}
        self._lock = asyncio.Lock()

    async def add(self, record: ReservationRecord) -> ReservationRecord:
        async with self._lock:
            self._items[record.id] = record
        return record

    async def get(self, reservation_id: str) -> Optional[ReservationRecord]:
        async with self._lock:
            return self._items.get(reservation_id)

    async def update(self, reservation_id: str, **fields: object) -> Optional[ReservationRecord]:
        async with self._lock:
            record = self._items.get(reservation_id)
            if record is None:
                return None
            updated = record.model_copy(update={**fields, "updated_at": _now()})
            self._items[reservation_id] = updated
            return updated

    async def set_status(
        self,
        reservation_id: str,
        status: ReservationStatus,
        *,
        exit_code: Optional[int] = None,
        error_msg: Optional[str] = None,
    ) -> Optional[ReservationRecord]:
        return await self.update(
            reservation_id,
            status=status,
            exit_code=exit_code,
            error_msg=error_msg,
        )

    async def delete(self, reservation_id: str) -> bool:
        async with self._lock:
            return self._items.pop(reservation_id, None) is not None

    async def list(self) -> list[ReservationRecord]:
        async with self._lock:
            return list(self._items.values())


# Single shared store instance.
store = ReservationStore()


def uptime_seconds() -> float:
    return time.monotonic() - START_MONOTONIC
