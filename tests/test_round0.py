"""
Round 0 regression tests — the flow-level fixes derived from production logs.

Run with:  pytest tests/test_round0.py

These are real unit tests (no browser, no network): the Playwright ``page`` is a
stub that serves canned HTML, which is enough to exercise every branch of trip
resolution. The delisted-trip case reproduces the 25 Jul 01:03:36 log exactly.
"""
from __future__ import annotations

import html as html_mod
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.models.schemas import RouteParams
from core.services import trip as trip_mod
from core.services.flow import _Timings
from core.services.trip import TripNotOffered, _resolve_trip_direct, resolve_trip
from core.utils.time_utils import (
    compute_arrival_datetime,
    compute_departure_datetime,
    relock_cutoff,
)


# ─────────────────────────────────────────────────────────────────
# Stubs
# ─────────────────────────────────────────────────────────────────
def _search_html(departures: list[str], date_ddmmyyyy: str) -> str:
    """Build a search page carrying lsServicos in the Vue ``:data`` attribute,
    the way mobifacil server-renders it."""
    payload = {
        "lsServicos": [
            {
                "saida": f"{date_ddmmyyyy} {hhmm}",
                "chegada": f"{date_ddmmyyyy} 05:35",
                "empresa": "VIACAO PIRACICABANA SA",
                "servico": f"83428-2026-07-25T{hhmm}-FARE-1",
                "preco": "137.97",
                "classe": "EXECUTIVO",
                "poltronasLivres": 20,
                "originId": "19052",
                "destinationId": "21787",
                "fareId": "FARE-1",
                "empresaId": "8",
            }
            for hhmm in departures
        ]
    }
    attr = html_mod.escape(json.dumps(payload), quote=True)
    return f'<html><body><list-trips :data="{attr}"></list-trips></body></html>'


class _Resp:
    def __init__(self, status: int, body: str = ""):
        self.status = status
        self._body = body

    def text(self) -> str:
        return self._body

    def json(self):
        return json.loads(self._body)


class _Request:
    """Records every URL fetched so tests can assert what was *not* attempted."""

    def __init__(self, responses):
        self._responses = responses
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        r = self._responses
        return r(url) if callable(r) else r


class _Page:
    def __init__(self, responses):
        self.request = _Request(responses)


def _params(departure: str = "01:05", date: str = "2026-07-25") -> RouteParams:
    return RouteParams(
        origin_id="19052",
        destination_id="-3",
        date=date,
        departure=departure,
        seat="15",
        date_formatted="25-07-2026",
        search_url="https://mobifacil.com.br/passagem-de-onibus/?origin=19052",
    )


# ─────────────────────────────────────────────────────────────────
# Finding A — delisted trip is a definitive negative, not a fault
# ─────────────────────────────────────────────────────────────────
# The exact departures mobifacil returned at 25 Jul 01:03:48, with 01:05 gone.
_LOG_DEPARTURES = ["02:05", "03:00", "03:05", "07:00", "08:00", "09:00", "10:00",
                   "11:00", "12:05", "14:00", "16:00", "17:00", "18:00"]


def test_delisted_departure_raises_trip_not_offered():
    """Reproduces the 25 Jul failure: 13 trips listed, ours absent."""
    page = _Page(_Resp(200, _search_html(_LOG_DEPARTURES, "25/07/2026")))
    with pytest.raises(TripNotOffered) as exc:
        _resolve_trip_direct(page, _params(departure="01:05"))
    assert "01:05" in str(exc.value)
    assert "no longer offered" in str(exc.value)


def test_date_rollover_raises_trip_not_offered():
    """Provider rolled over to the next day — every listed trip is for 26/07."""
    page = _Page(_Resp(200, _search_html(["02:05", "07:00"], "26/07/2026")))
    with pytest.raises(TripNotOffered):
        _resolve_trip_direct(page, _params(departure="01:05", date="2026-07-25"))


def test_resolve_trip_does_not_fall_back_when_trip_is_not_offered(monkeypatch):
    """The key behaviour change: a delisted trip must NOT drag the flow through the
    XHR-intercept fallback, which looks for a trip card that cannot exist."""
    called = []
    monkeypatch.setattr(
        trip_mod, "_resolve_trip_via_intercept",
        lambda page, params: called.append("intercept") or {},
    )
    page = _Page(_Resp(200, _search_html(_LOG_DEPARTURES, "25/07/2026")))

    with pytest.raises(TripNotOffered):
        resolve_trip(page, _params(departure="01:05"))
    assert called == [], "intercept fallback must be skipped for a delisted trip"


# ── ...but a genuine failure must still fall back ────────────────
def test_unparseable_html_still_falls_back(monkeypatch):
    """No lsServicos at all could mean the page structure changed — that is NOT
    conclusive, so it must fall back rather than declare the trip gone."""
    monkeypatch.setattr(
        trip_mod, "_resolve_trip_via_intercept",
        lambda page, params: {"serviceId": "fallback"},
    )
    page = _Page(_Resp(200, "<html><body>no data attribute here</body></html>"))
    assert resolve_trip(page, _params())["serviceId"] == "fallback"


def test_http_error_still_falls_back(monkeypatch):
    monkeypatch.setattr(
        trip_mod, "_resolve_trip_via_intercept",
        lambda page, params: {"serviceId": "fallback"},
    )
    page = _Page(_Resp(503))
    assert resolve_trip(page, _params())["serviceId"] == "fallback"


def test_present_departure_proceeds_to_bus_details():
    """Sanity: the happy path still reaches the BusDetails fetch."""
    def _responses(url):
        if "BusDetails" in url:
            return _Resp(503)          # stop here; we only assert we got this far
        return _Resp(200, _search_html(["01:05"] + _LOG_DEPARTURES, "25/07/2026"))

    page = _Page(_responses)
    assert _resolve_trip_direct(page, _params(departure="01:05")) is None
    assert any("BusDetails" in u for u in page.request.calls)


# ─────────────────────────────────────────────────────────────────
# Finding A — end-to-end wiring: exit 3, no profile reset, no retry
# ─────────────────────────────────────────────────────────────────
class _Ctx:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class _FlowPage(_Page):
    def on(self, event, cb):
        pass


def test_execute_flow_returns_exit_3_for_a_delisted_trip(monkeypatch):
    from core.services import flow as flow_mod

    ctx = _Ctx(_FlowPage(_Resp(200, "")))
    monkeypatch.setattr(flow_mod, "build_context", lambda pw: ctx)
    monkeypatch.setattr(flow_mod, "open_search_page", lambda page, params: None)
    monkeypatch.setattr(
        flow_mod, "resolve_trip",
        lambda page, params: (_ for _ in ()).throw(TripNotOffered("01:05 gone")),
    )

    code, trip = flow_mod._execute_flow(object(), _params(), is_relock=True)
    assert (code, trip) == (3, None)
    assert ctx.closed, "browser context must still be closed on the exit-3 path"


def test_run_flow_does_not_reset_profile_or_retry_on_exit_3(monkeypatch):
    """The heart of finding A: a delisted trip must not cost a profile wipe (which
    destroys the anti-bot session) plus a second, equally doomed, full flow."""
    from contextlib import contextmanager

    from core.services import flow as flow_mod

    resets, runs = [], []

    @contextmanager
    def _fake_pw():
        yield object()

    monkeypatch.setattr(flow_mod, "sync_playwright", _fake_pw)
    monkeypatch.setattr(flow_mod, "_profile_looks_valid", lambda d: True)
    monkeypatch.setattr(flow_mod, "reset_profile", lambda d: resets.append(d))
    monkeypatch.setattr(
        flow_mod, "_execute_flow",
        lambda pw, params, is_relock: (runs.append(1), (3, None))[1],
    )

    assert flow_mod.run_flow(_params(), "testid", True) == (3, None)
    assert resets == [], "exit 3 must NOT wipe the browser profile"
    assert len(runs) == 1, "exit 3 must NOT trigger the retry"


def test_run_flow_still_resets_and_retries_on_a_genuine_error(monkeypatch):
    """Guard the other side: real faults keep their reset-and-retry behaviour."""
    from contextlib import contextmanager

    from core.services import flow as flow_mod

    resets, runs = [], []

    @contextmanager
    def _fake_pw():
        yield object()

    monkeypatch.setattr(flow_mod, "sync_playwright", _fake_pw)
    monkeypatch.setattr(flow_mod, "_profile_looks_valid", lambda d: True)
    monkeypatch.setattr(flow_mod, "reset_profile", lambda d: resets.append(d))
    monkeypatch.setattr(flow_mod, "jitter", lambda a, b: None)
    monkeypatch.setattr(
        flow_mod, "_execute_flow",
        lambda pw, params, is_relock: (runs.append(1), (2, None))[1],
    )

    assert flow_mod.run_flow(_params(), "testid", True) == (2, None)
    assert len(resets) == 1 and len(runs) == 2


# ─────────────────────────────────────────────────────────────────
# Finding E — pre-departure cutoff
# ─────────────────────────────────────────────────────────────────
def test_relock_cutoff_sits_before_departure():
    dep = datetime(2026, 7, 25, 1, 5, tzinfo=timezone.utc)
    assert relock_cutoff(dep, 15) == dep - timedelta(minutes=15)
    assert relock_cutoff(dep, 0) == dep


def test_cutoff_would_have_stopped_the_failing_run():
    """The 25 Jul job fired at 01:03:36 for a 01:05 departure — 89 s out. With a
    15-minute cutoff it would have expired instead of running."""
    dep = datetime(2026, 7, 25, 1, 5, tzinfo=timezone.utc)
    fired_at = datetime(2026, 7, 25, 1, 3, 36, tzinfo=timezone.utc)
    assert fired_at >= relock_cutoff(dep, 15)
    # ...while the healthy 00:42:36 run is comfortably inside the window.
    assert datetime(2026, 7, 25, 0, 42, 36, tzinfo=timezone.utc) < relock_cutoff(dep, 15)


# ─────────────────────────────────────────────────────────────────
# Timing instrumentation
# ─────────────────────────────────────────────────────────────────
def test_timings_accumulate_and_format():
    t = _Timings()
    with t.step("launch"):
        pass
    with t.step("lock"):
        pass
    with t.step("lock"):          # repeated labels accumulate
        pass
    s = t.summary(total=12.0)
    assert s.startswith("total=12.0s")
    assert "launch=" in s and "lock=" in s


def test_timings_record_a_step_that_raised():
    """Failure timings are the ones most worth having."""
    t = _Timings()
    with pytest.raises(ValueError):
        with t.step("resolve_trip"):
            raise ValueError("boom")
    assert "resolve_trip=" in t.summary(total=1.0)


# ─────────────────────────────────────────────────────────────────
# API level — exit 3 surfaces as 409 + expired, with no re-lock job
# ─────────────────────────────────────────────────────────────────
def test_post_reservations_maps_exit_3_to_expired(monkeypatch):
    from fastapi.testclient import TestClient

    from core.api import routes as routes_mod
    from core.main import app

    scheduled = []
    monkeypatch.setattr(routes_mod, "run_flow", lambda params, rid: (3, None))
    monkeypatch.setattr(routes_mod, "schedule_relock", lambda *a, **k: scheduled.append(a))

    # A departure comfortably inside the 48 h window the endpoint enforces.
    dep = datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/Sao_Paulo")
    ) + timedelta(hours=6)

    with TestClient(app) as client:
        r = client.post("/reservations", json={
            "id": "t3st0003",
            "origin_id": "19052", "destination_id": "-3",
            "date": dep.strftime("%Y-%m-%d"), "departure": dep.strftime("%H:%M"),
            "seat": "15",
        })

    assert r.status_code == 409, r.text
    body = r.json()
    assert body["status"] == "expired"
    assert body["exit_code"] == 3
    assert "no longer offered" in (body["error_msg"] or "")
    assert scheduled == [], "a withdrawn trip must not get a re-lock job"


# ─────────────────────────────────────────────────────────────────
# Pre-existing time helpers (pinned while we are here)
# ─────────────────────────────────────────────────────────────────
def test_departure_datetime_is_sao_paulo_local():
    assert compute_departure_datetime("2026-05-28", "00:00").isoformat() == "2026-05-28T03:00:00+00:00"


def test_arrival_datetime_handles_overnight():
    arr = compute_arrival_datetime("2026-07-24", "23:30", "05:15")
    dep = compute_departure_datetime("2026-07-24", "23:30")
    assert arr > dep and (arr - dep) == timedelta(hours=5, minutes=45)
