"""
FastAPI application entrypoint.

Bootstraps a virtual X display (xvfb) for headed Chromium on Linux, configures
the lifespan (start/stop the scheduler) and mounts the API router.

Run via: ``uvicorn core.main:app --host 127.0.0.1 --port 8771 --workers 1``
(see core/start.sh, which also handles the xvfb wrapping for the whole process).
"""
from __future__ import annotations

import os
import sys

from .config import settings
from .utils.logger import log


def _bootstrap_xvfb() -> None:
    """
    Re-exec the process under ``xvfb-run`` when no X display is present.

    FIX (bug 8): the original ran ``os.execvp`` at module top *before* logging
    existed, so a missing ``xvfb-run`` binary killed the process silently. Here
    we short-circuit when not needed, restrict to POSIX, and wrap the exec so the
    failure is logged before exiting.
    """
    if settings.headless:
        return  # no display needed in headless mode
    if sys.platform == "win32" or os.name == "nt":
        return  # xvfb is POSIX-only; Chromium runs headed on Windows directly
    if os.environ.get("DISPLAY") or os.environ.get("_XVFB_RUNNING"):
        return  # a display (real or already-wrapped) is available

    os.environ["_XVFB_RUNNING"] = "1"
    try:
        os.execvp("xvfb-run", ["xvfb-run", "-a", sys.executable] + sys.argv)
    except (FileNotFoundError, OSError) as exc:
        log.error(
            f"[bootstrap] xvfb-run unavailable ({exc!r}); cannot create a virtual "
            "display. Install xvfb (e.g. `apt-get install xvfb`) or set HEADLESS=true."
        )
        sys.exit(2)


_bootstrap_xvfb()

# Imports below intentionally follow the xvfb bootstrap so a re-exec happens
# before the heavier modules (Playwright, APScheduler) are imported.
from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI  # noqa: E402

from .api.admin import admin_router  # noqa: E402
from .api.routes import router  # noqa: E402
from .scheduler.jobs import shutdown_scheduler, start_scheduler  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("[lifespan] starting bus-reserver service")
    start_scheduler()
    try:
        yield
    finally:
        # Graceful shutdown — don't block the event loop on in-flight jobs.
        shutdown_scheduler()
        log.info("[lifespan] service stopped")


app = FastAPI(
    title="BusBooker",
    description="busbooker.",
    version="7.0.0",
    lifespan=lifespan,
)
app.include_router(router)
app.include_router(admin_router, prefix="/admin")
