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

# Monotonic process start, used for the /health uptime figure.
START_MONOTONIC: float = time.monotonic()

# Statuses past which a record must never be silently reverted (see
# ``update(only_if_active=...)``). Reaching one of these is a deliberate,
# terminal decision (user/admin cancel, departure expiry).
_TERMINAL_STATUSES = (ReservationStatus.cancelled, ReservationStatus.expired)

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

    async def update(
        self,
        reservation_id: str,
        *,
        only_if_active: bool = False,
        **fields: object,
    ) -> Optional[ReservationRecord]:
        """Patch a record. Returns the updated record, or ``None`` if it is gone.

        When ``only_if_active`` is set, a record that has already reached a
        terminal status (cancelled/expired) is left untouched and ``None`` is
        returned. This prevents a long-running browser flow from resurrecting a
        reservation that was cancelled or expired *while the flow was running*.
        """
        async with self._lock:
            record = self._items.get(reservation_id)
            if record is None:
                return None
            if only_if_active and record.status in _TERMINAL_STATUSES:
                return None
            updated = record.model_copy(update={**fields, "updated_at": _now()})
            self._items[reservation_id] = updated
            return updated

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
