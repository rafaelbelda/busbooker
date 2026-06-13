"""
Structured logging for the whole service.

Single logger named ``busbooker`` writing to stdout, stderr and a rotating
file. All service modules do ``from ..utils.logger import log``.
"""
from __future__ import annotations

import contextvars
import logging
import re
import sys
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, Optional

from ..config import settings

_LOGGER_NAME = "busbooker"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Set for the duration of one reservation flow so a per-reservation file handler
# can pick out only that flow's records (the logger is process-wide and other
# requests log concurrently). Lives in the flow's thread context.
_current_reservation: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "busbooker_reservation_id", default=None
)


def _force_utf8(stream) -> None:
    """
    Make a console stream tolerate non-ASCII log output.

    The format separator (—) and arrow glyph (→) are non-ASCII; on a non-UTF-8
    console (e.g. Windows cp1252) the StreamHandler would raise
    UnicodeEncodeError on every line. Reconfigure to UTF-8 with replacement.
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


def _build_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:  # already configured (e.g. reload) — don't double-attach
        return logger

    _force_utf8(sys.stdout)
    _force_utf8(sys.stderr)

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT, _DATEFMT)

    # stdout: DEBUG..WARNING inclusive.
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.setFormatter(formatter)
    # FIX (bug 1): the original dropped WARNING entirely — stdout filtered
    # `levelno <= INFO` while stderr started at ERROR, so WARNING (30) fell
    # through both handlers. Raise the stdout ceiling to WARNING inclusive.
    stdout_handler.addFilter(lambda r: r.levelno <= logging.WARNING)

    # stderr: ERROR and above.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(formatter)

    # Rotating file: everything at DEBUG.
    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)
    logger.addHandler(file_handler)
    return logger


log: logging.Logger = _build_logger()


# ─────────────────────────────────────────────────────────────────
# Per-reservation log files
# ─────────────────────────────────────────────────────────────────
class _ReservationFilter(logging.Filter):
    """Pass only records emitted while *this* reservation's flow is the active one.

    Scoped via ``_current_reservation`` (a context var) rather than the thread id,
    so records from concurrent unrelated requests (/seats, /health) — handled on
    other threads where the var is unset — never leak into the file.
    """

    def __init__(self, reservation_id: str) -> None:
        super().__init__()
        self._rid = reservation_id

    def filter(self, record: logging.LogRecord) -> bool:
        return _current_reservation.get() == self._rid


def reservation_log_path(reservation_id: str) -> Path:
    """Absolute path of a reservation's dedicated log file."""
    return Path(settings.reservation_log_dir) / f"{reservation_id}.log"


# Reservation ids are short hex/uuid fragments; clients may supply their own.
# Validate before using one as a filename so a crafted id can't escape the dir.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def read_reservation_log(reservation_id: str, tail_kb: int = 256) -> Optional[dict]:
    """Read a reservation's flow log as a JSON-able dict (``exists``/``truncated``/
    ``size``/``content``), returning only the last ``tail_kb`` KB.

    Returns ``None`` when the id is unsafe or its path would escape the log dir —
    callers should treat that as a 404. A missing file is not an error: the dict
    is returned with ``exists: False`` and empty content (the flow hasn't run yet).
    """
    if not reservation_id or not _SAFE_ID.match(reservation_id):
        return None
    path = reservation_log_path(reservation_id)
    base = Path(settings.reservation_log_dir).resolve()
    try:
        path.resolve().relative_to(base)
    except (ValueError, OSError):
        return None
    if not path.exists():
        return {"reservation_id": reservation_id, "exists": False,
                "truncated": False, "size": 0, "content": ""}
    raw = path.read_text(encoding="utf-8", errors="replace")
    size = len(raw)
    max_chars = max(1, tail_kb) * 1024
    truncated = size > max_chars
    return {
        "reservation_id": reservation_id,
        "exists": True,
        "truncated": truncated,
        "size": size,
        "content": raw[-max_chars:] if truncated else raw,
    }


@contextmanager
def reservation_log(reservation_id: Optional[str]) -> Iterator[Optional[Path]]:
    """Tee the current flow's log records into ``<log dir>/<id>.log``.

    Records still go to stdout/stderr/app.log as before — this only ADDS a
    per-reservation file (DEBUG and up) to make a single booking easy to trace.
    The file is appended to, so an initial lock and all later re-locks share one
    chronological history. A no-op when ``reservation_id`` is falsy.
    """
    if not reservation_id:
        yield None
        return

    path = reservation_log_path(reservation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    handler.addFilter(_ReservationFilter(reservation_id))

    token = _current_reservation.set(reservation_id)
    log.addHandler(handler)
    try:
        log.info(f"───── flow run started · reservation {reservation_id} ─────")
        yield path
    finally:
        log.info(f"───── flow run ended · reservation {reservation_id} ─────")
        log.removeHandler(handler)
        handler.close()
        _current_reservation.reset(token)
