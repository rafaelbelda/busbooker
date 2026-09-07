"""
Round 2 regression tests — reliability floor.

Hermetic: no Chromium, no network, no reservations. See tests/conftest.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.models.schemas import ReservationRecord, ReservationStatus


def _now():
    return datetime.now(timezone.utc)


def _record(rid="aaaa1111", **kw) -> ReservationRecord:
    base = dict(
        id=rid, origin_id="19052", destination_id="-3", date="2026-07-25",
        departure="09:00", seat="15", created_at=_now(), updated_at=_now(),
    )
    base.update(kw)
    return ReservationRecord(**base)


# ─────────────────────────────────────────────────────────────────
# #11 — a hung flow must not wedge the service forever
# ─────────────────────────────────────────────────────────────────
def test_flow_timeout_returns_exit_2_and_releases_the_lock(monkeypatch):
    """The whole point: one stuck flow used to hold FLOW_LOCK forever, blocking
    every later reservation AND every re-lock for every other reservation."""
    import time as _time

    from core.config import settings
    from core.services import flow as flow_mod
    from core.state import FLOW_LOCK

    monkeypatch.setattr(settings, "flow_timeout_seconds", 0.2)
    # A flow that hangs well past the budget (short, so the suite stays fast --
    # the assertion is about the budget being enforced, not about the duration).
    monkeypatch.setattr(flow_mod, "run_flow",
                        lambda params, rid, relock: _time.sleep(2) or (0, {}))

    async def go():
        code, trip = await flow_mod.run_flow_guarded(None, "stuck123", False)
        assert (code, trip) == (2, None)
        # Lock released despite the worker thread still running.
        assert not FLOW_LOCK.locked()
        # And a later caller is not blocked by the abandoned wait.
        assert await asyncio.wait_for(_try_lock(), timeout=1.0)

    async def _try_lock():
        async with FLOW_LOCK:
            return True

    asyncio.run(go())


def test_successful_flow_passes_through_the_guard(monkeypatch):
    from core.services import flow as flow_mod

    monkeypatch.setattr(flow_mod, "run_flow",
                        lambda params, rid, relock: (0, {"serviceId": "83428"}))

    async def go():
        return await flow_mod.run_flow_guarded(None, "ok123456", True)

    code, trip = asyncio.run(go())
    assert code == 0 and trip["serviceId"] == "83428"


def test_current_flow_is_observable_while_running(monkeypatch):
    """A wedged flow was completely invisible; /health now reports it."""
    from core.services import flow as flow_mod
    from core.state import current_flow_info

    seen = {}

    def _capture(params, rid, relock):
        seen["during"] = current_flow_info()
        return (0, None)

    monkeypatch.setattr(flow_mod, "run_flow", _capture)
    assert current_flow_info() is None
    asyncio.run(flow_mod.run_flow_guarded(None, "watch001", False))
    assert seen["during"]["reservation_id"] == "watch001"
    assert seen["during"]["running_seconds"] >= 0
    assert current_flow_info() is None, "must be cleared after the flow"


def test_flows_use_a_dedicated_single_thread_executor():
    """Shared with the default pool, slow flows could starve /seats and /search;
    and more than one worker would let a timed-out flow race the next one against
    the same Chromium profile."""
    from core.state import FLOW_EXECUTOR
    assert FLOW_EXECUTOR._max_workers == 1


# ─────────────────────────────────────────────────────────────────
# #13 — soft-fail backoff must escalate, not hammer
# ─────────────────────────────────────────────────────────────────
def test_soft_fail_backoff_escalates_and_caps():
    from core.config import settings
    from core.scheduler.jobs import _soft_fail_delay

    assert _soft_fail_delay(1) == 5
    assert _soft_fail_delay(2) == 10
    # Doubling, capped at the normal cadence — never faster than 5 min, never
    # slower than the interval.
    for n in range(1, 12):
        assert 5 <= _soft_fail_delay(n) <= settings.scheduler_interval
    assert _soft_fail_delay(9) == settings.scheduler_interval


def test_backoff_is_monotonic():
    from core.scheduler.jobs import _soft_fail_delay
    seq = [_soft_fail_delay(n) for n in range(1, 10)]
    assert seq == sorted(seq)


def test_old_behaviour_would_have_hammered():
    """Pins why this matters: a flat 5-minute retry over a 48 h window is ~570
    full browser flows against the provider."""
    from core.scheduler.jobs import _soft_fail_delay

    window_minutes = 48 * 60
    flat = window_minutes / 5
    backed_off = sum(1 for _ in _simulate(window_minutes, _soft_fail_delay))
    assert flat > 500
    assert backed_off < flat / 2


def _simulate(window_minutes, delay_fn):
    t, n = 0, 0
    while t < window_minutes:
        n += 1
        t += delay_fn(n)
        yield t


def test_consecutive_failures_field_defaults_to_zero():
    assert _record().consecutive_failures == 0


# ─────────────────────────────────────────────────────────────────
# #14 — /seats and /search were entirely unguarded
# ─────────────────────────────────────────────────────────────────
def test_seats_and_search_are_rate_limited(monkeypatch, client, stub_provider):
    from core.config import settings
    from core.utils import ratelimit

    monkeypatch.setattr(settings, "rate_limit_per_min", 2)
    monkeypatch.setattr(ratelimit, "_hits", {})

    # Third call in the window is refused before any provider work happens.
    codes = [client.get("/seats", params={
        "origin_id": "19052", "destination_id": "-3",
        "date": "2026-07-25", "departure": "09:00",
    }).status_code for _ in range(3)]
    assert codes[-1] == 429


def test_search_shares_the_same_limit(monkeypatch, client, stub_provider):
    from core.config import settings
    from core.utils import ratelimit

    monkeypatch.setattr(settings, "rate_limit_per_min", 1)
    monkeypatch.setattr(ratelimit, "_hits", {})

    body = {"url": "https://mobifacil.com.br/passagem-de-onibus/?origin=1&destination=2&date=25-07-2026"}
    client.post("/search", json=body)
    assert client.post("/search", json=body).status_code == 429


def test_read_endpoints_do_not_consume_the_browser_queue(monkeypatch, client, stub_provider):
    """The queue cap protects the single browser. If /seats consumed it, a burst of
    slow reservations would 503 every seat-map read."""
    from core.config import settings
    from core.utils import ratelimit

    monkeypatch.setattr(settings, "rate_limit_per_min", 0)
    monkeypatch.setattr(settings, "max_flow_queue", 1)
    monkeypatch.setattr(ratelimit, "_in_flight", 99)   # queue "full"

    r = client.get("/seats", params={
        "origin_id": "19052", "destination_id": "-3",
        "date": "2026-07-25", "departure": "09:00",
    })
    assert r.status_code != 503


# ─────────────────────────────────────────────────────────────────
# #16 — retention
# ─────────────────────────────────────────────────────────────────
def test_purge_removes_only_long_departed_reservations(tmp_log_dir):
    from core.state import ReservationStore

    async def go():
        store = ReservationStore()
        await store.add(_record("old00001", departure_datetime=_now() - timedelta(days=30)))
        await store.add(_record("recent01", departure_datetime=_now() - timedelta(days=1)))
        await store.add(_record("future01", departure_datetime=_now() + timedelta(days=1)))
        await store.add(_record("nodate01", departure_datetime=None))

        assert await store.purge_old(7) == 1
        remaining = {r.id for r in await store.list()}
        assert remaining == {"recent01", "future01", "nodate01"}

    asyncio.run(go())


def test_purge_is_disabled_by_zero(tmp_log_dir):
    from core.state import ReservationStore

    async def go():
        store = ReservationStore()
        await store.add(_record("old00001", departure_datetime=_now() - timedelta(days=999)))
        assert await store.purge_old(0) == 0
        assert len(await store.list()) == 1

    asyncio.run(go())


def test_purge_deletes_the_reservation_log_file(tmp_log_dir):
    from core.state import ReservationStore
    from core.utils.logger import reservation_log_path

    async def go():
        store = ReservationStore()
        await store.add(_record("purgeme1", departure_datetime=_now() - timedelta(days=30)))
        path = reservation_log_path("purgeme1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("flow log contents", encoding="utf-8")
        assert path.exists()

        await store.purge_old(7)
        assert not path.exists(), "log files grew forever alongside the rows"

    asyncio.run(go())


def test_store_close_is_callable():
    """ReservationDB.close() existed but nothing ever called it."""
    from core.state import ReservationStore
    ReservationStore().close()          # no db attached — must not raise


# ─────────────────────────────────────────────────────────────────
# D — basket state must be logged, not inferred
# ─────────────────────────────────────────────────────────────────
def test_lock_seat_logs_provider_basket_state(caplog, no_sleep):
    import logging

    from core.models.schemas import RouteParams
    from core.services import seat as seat_mod

    class _Resp:
        status = 200
        def json(self):
            return {"success": True, "seatUUID": "abc123",
                    "quantityTotal": 4, "count": 4, "total": 435.92}

    class _Page:
        class request:
            @staticmethod
            def post(*a, **kw):
                return _Resp()

    params = RouteParams(origin_id="19052", destination_id="-3", date="2026-07-25",
                         departure="09:00", seat="15", date_formatted="25-07-2026",
                         search_url="https://mobifacil.com.br/x")
    trip = {"service": "s", "empresaId": "8", "fareId": "FARE-1", "seatMap": []}

    with caplog.at_level(logging.INFO, logger="busbooker"):
        assert seat_mod.lock_seat_api(_Page(), trip, params) == "abc123"

    text = caplog.text
    assert "quantityTotal=4" in text and "count=4" in text
    # A basket holding more than this reservation's seat is the signal that every
    # reservation shares one provider cart.
    assert "may be accumulating" in text
