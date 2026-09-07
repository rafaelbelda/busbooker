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

from .config import settings
from .models.schemas import ReservationRecord, ReservationStatus
from .persistence import ReservationDB
from .utils.logger import log
from .utils.time_utils import relock_cutoff

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
    """UUID-keyed reservation store protected by a single asyncio lock.

    When a ``ReservationDB`` is attached, every mutation is written through to
    SQLite so reservations survive a restart; ``load()`` restores them.
    """

    def __init__(self, db: Optional[ReservationDB] = None) -> None:
        self._items: dict[str, ReservationRecord] = {}
        self._lock = asyncio.Lock()
        self._db = db

    async def add(
        self, record: ReservationRecord, *, replace: bool = False
    ) -> Optional[ReservationRecord]:
        """Insert a reservation. Returns ``None`` if the id is already taken.

        Clients may supply their own id, so a duplicate is reachable from the public
        API. Silently overwriting was the old behaviour and it orphaned state: the
        previous record's ``relock_<id>`` job keeps running against the *new*
        record's route, re-locking a seat nobody asked for. Rejecting is race-free
        because the check and the insert share this lock.
        """
        async with self._lock:
            if not replace and record.id in self._items:
                return None
            self._items[record.id] = record
            if self._db is not None:
                self._db.upsert(record)
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
            if self._db is not None:
                self._db.upsert(updated)
            return updated

    async def delete(self, reservation_id: str) -> bool:
        async with self._lock:
            existed = self._items.pop(reservation_id, None) is not None
            if existed and self._db is not None:
                self._db.delete(reservation_id)
            return existed

    async def list(self) -> list[ReservationRecord]:
        async with self._lock:
            return list(self._items.values())

    async def load(self) -> None:
        """Restore persisted reservations and reconcile state lost to the restart.

        * ``pending`` → ``failed`` ("interrupted by restart"): the flow that owned
          a pending record died with the previous process.
        * ``locked`` / ``failed`` whose departure already passed → ``expired``: the
          bus left while we were down.

        Reconciled changes are persisted. Re-lock jobs are re-armed separately by
        ``scheduler.jobs.rehydrate_relocks`` (the store stays scheduler-agnostic).
        """
        if self._db is None:
            return
        records = self._db.load_all()
        now = _now()
        async with self._lock:
            for rec in records:
                new_status, error = rec.status, rec.error_msg
                if rec.status == ReservationStatus.pending:
                    new_status, error = ReservationStatus.failed, "interrupted by restart"
                elif (
                    rec.status in (ReservationStatus.locked, ReservationStatus.failed)
                    and rec.departure_datetime is not None
                    and now >= relock_cutoff(
                        rec.departure_datetime,
                        settings.relock_stop_minutes_before_departure,
                    )
                ):
                    # Same pre-departure cutoff the scheduler uses, so a restart
                    # agrees with a running process about what is still live.
                    new_status = ReservationStatus.expired
                if new_status != rec.status:
                    rec = rec.model_copy(
                        update={"status": new_status, "error_msg": error, "updated_at": now}
                    )
                    self._db.upsert(rec)
                self._items[rec.id] = rec
        log.info(f"[persistence] restored {len(records)} reservation(s) from disk")


# Single shared store instance, durable via SQLite.
store = ReservationStore(db=ReservationDB(settings.reservation_db))


def uptime_seconds() -> float:
    return time.monotonic() - START_MONOTONIC
