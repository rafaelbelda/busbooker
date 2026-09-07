"""
Shared pytest configuration.

**Every test in this suite is hermetic.** Nothing here launches Chromium, opens a
socket to mobifacil, or creates a real reservation — a real flow run books an
actual bus seat on a live third-party site, adds it to the provider's basket, and
contributes to anti-bot/ban risk. There is no sandbox to point at.

The pattern to follow when adding tests:

* stub the Playwright ``page`` (an object exposing ``.request.get/post`` that
  returns canned responses) instead of launching a browser;
* monkeypatch ``core.api.routes.run_flow_guarded`` for API-level tests;
* use the ``client`` fixture below, which skips the app lifespan entirely.

Environment is pinned before ``core`` is imported so a developer's real ``.env``
(which may point at the production SQLite file) can never be picked up. Explicit
environment variables win over ``.env`` in pydantic-settings, and ``setdefault``
means an intentional override from the shell still works.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("RESERVATION_DB", ":memory:")

# Allow `pytest` from anywhere in the repo, not just the root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _block_the_network(monkeypatch):
    """Hard guard: fail any test that tries to open a real socket.

    Autouse and unconditional. A test that reaches mobifacil does real damage —
    it can lock a real seat — and the mistake is easy to make by accident: calling
    an endpoint like ``/seats`` looks harmless right up until it fans out to the
    provider. Rather than trusting every future test to remember, connections are
    blocked at the socket layer and the failure names the host it tried to reach.

    Loopback is left open so ``TestClient`` and anything genuinely local still work.
    """
    import socket

    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(
                f"Blocked a real network connection to {host!r} during tests. "
                "Tests must never reach the provider: a live flow books an actual "
                "seat. Stub the call (see tests/conftest.py)."
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def stub_provider(monkeypatch):
    """Replace the two provider-facing helpers behind /seats and /search."""
    from core.api import routes as routes_mod
    from core.models.schemas import SeatInfo

    monkeypatch.setattr(
        routes_mod, "fetch_seat_map",
        lambda params: ([SeatInfo(number="15", available=True)], []),
    )
    monkeypatch.setattr(routes_mod, "search_trips", lambda params: [])


@pytest.fixture(scope="session")
def client():
    """TestClient WITHOUT the app lifespan.

    The scheduler is a module-level ``AsyncIOScheduler`` that binds to whichever
    event loop starts it, so running the lifespan more than once per process
    leaves it pointing at a closed loop. These tests exercise routing, validation
    and auth — none of which need the scheduler or the startup restore — so we
    skip startup rather than working around it.
    """
    from fastapi.testclient import TestClient

    from core.main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def tmp_log_dir(tmp_path, monkeypatch):
    """Redirect the reservation log directory into a throwaway path.

    Autouse: any test that exercises a flow writes a per-reservation log, and
    without this it lands in the repo (or worse, the real log directory). Tests
    that need the path can request the fixture by name.
    """
    from core.config import settings

    d = tmp_path / "reservations"
    d.mkdir()
    monkeypatch.setattr(settings, "reservation_log_dir", str(d))
    return d


@pytest.fixture
def no_sleep(monkeypatch):
    """Make retry/backoff sleeps instant so timing tests stay fast."""
    from core.services import browser as browser_mod

    monkeypatch.setattr(browser_mod.time, "sleep", lambda s: None)
