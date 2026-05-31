"""
Structured logging for the whole service.

Single logger named ``busbooker`` writing to stdout, stderr and a rotating
file. All service modules do ``from ..utils.logger import log``.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..config import settings

_LOGGER_NAME = "busbooker"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


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
