"""
Durable reservation storage (stdlib ``sqlite3``, JSON-blob schema).

This is a single-worker service, so a single SQLite file is exactly the right
amount of database: ACID writes, crash-safe under WAL, no extra dependency and no
migration framework. Each reservation is stored as its ``ReservationRecord`` JSON
in one ``data`` column, so the table never needs migrating when the model grows.

All calls happen under the ReservationStore's asyncio lock (one event-loop
thread) and are short; a single connection guarded by a ``threading.Lock`` (in
case a future maintenance job ever touches it from an executor thread) is enough.
``synchronous=NORMAL`` under WAL avoids an fsync on every commit while staying
crash-safe, keeping the inline writes cheap.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import List

from .models.schemas import ReservationRecord
from .utils.logger import log


class ReservationDB:
    def __init__(self, path: str) -> None:
        self._path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        self._conn.commit()
        log.info(f"[persistence] reservation store at {path}")

    def upsert(self, record: ReservationRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO reservations (id, data) VALUES (?, ?)",
                (record.id, record.model_dump_json()),
            )
            self._conn.commit()

    def delete(self, reservation_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,))
            self._conn.commit()

    def load_all(self) -> List[ReservationRecord]:
        """Read every stored record. A single unreadable row is skipped (logged)
        rather than blocking startup."""
        with self._lock:
            rows = self._conn.execute("SELECT id, data FROM reservations").fetchall()
        records: List[ReservationRecord] = []
        for rid, data in rows:
            try:
                # `is_expired` is a computed field in the JSON; pydantic ignores
                # it on input (extra='ignore') and recomputes it.
                records.append(ReservationRecord.model_validate_json(data))
            except Exception as exc:
                log.error(f"[persistence] skipping unreadable record {rid}: {exc!r}")
        return records

    def close(self) -> None:
        with self._lock:
            self._conn.close()
