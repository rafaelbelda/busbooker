"""
Round 3 — #24 (one seat-map module) and #1 (trip identity).

Hermetic: no Chromium, no network, no reservations. See tests/conftest.py.
"""
from __future__ import annotations

import html as html_mod
import json

import pytest

from core.models.schemas import RouteParams
from core.services import trip as trip_mod
from core.services.htmlsearch import (
    ls_departure,
    ls_service_id,
    select_trip,
)
from core.services.seatmap import (
    build_decks,
    find_seat,
    flatten,
    has_divider,
    iter_seats,
    normalise_seat_number,
    raw_label,
    seat_matches,
)
from core.services.trip import TripNotOffered, _parse_bus_details, _resolve_trip_direct


# ─────────────────────────────────────────────────────────────────
# #24 — one module owns the seatMap format
# ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("01", "1"), ("5", "5"), ("5.0", "5"), (" 07 ", "7"), (12, "12"),
    ("-99", None), (-99, None), ("WC", None), ("ES", None), ("GE", None),
    ("", None), (None, None),
])
def test_normalise_seat_number(raw, expected):
    assert normalise_seat_number(raw) == expected


@pytest.mark.parametrize("raw,wanted", [("05", "5"), ("5", "05"), ("05", "05"), ("5", "5")])
def test_seat_matches_across_padding(raw, wanted):
    assert seat_matches(raw, wanted) is True


@pytest.mark.parametrize("raw,wanted", [("05", "6"), ("-99", "99"), ("WC", "0"), ("05", "")])
def test_seat_does_not_match(raw, wanted):
    assert seat_matches(raw, wanted) is False


def _seat(numero, disponivel=True, idoso=False):
    return {"disponivel": disponivel, "numero": numero, "idoso": idoso,
            "label": str(numero), "x": "1", "y": "0", "z": "0"}


AISLE = {"numero": -99, "disponivel": False}
DECKER = [
    [_seat("01"), AISLE, _seat("02")],
    [],                                     # divider: everything after is FLOOR 1
    [_seat("03"), AISLE, _seat("04")],
]


def test_deck_index_comes_from_the_divider_not_the_z_field():
    """Production data has z="0" on EVERY seat including the upper deck, so
    trusting it collapsed a double-decker into one floor."""
    assert has_divider(DECKER) is True
    assert [s.deck for s in flatten(DECKER)] == [0, 0, 1, 1]


def test_flatten_uses_array_indices_as_coordinates():
    seats = flatten(DECKER)
    assert (seats[0].depth, seats[0].cross) == (0, 0)
    assert (seats[1].depth, seats[1].cross) == (0, 2)


def test_flatten_drops_aisles_and_landmarks():
    m = [[_seat("01"), AISLE, {"numero": "WC"}, {"numero": "ES"}, _seat("02")]]
    assert [s.number for s in flatten(m)] == ["1", "2"]


def test_iter_seats_tolerates_malformed_rows():
    assert len(list(iter_seats([_seat("01"), None, ["x", _seat("02")]]))) == 1


def test_find_seat_and_raw_label():
    assert find_seat(DECKER, "3")["numero"] == "03"
    assert find_seat(DECKER, "99") is None
    assert raw_label(DECKER, "3") == "03"       # LockSeat matches on the padded form
    assert raw_label(DECKER, "99") == "99"      # unknown seat falls back


def test_all_consumers_agree_on_the_same_map():
    """The three parsers this module replaced used to disagree — that is what let
    the double-decker bug exist in one of them and not the others."""
    from core.services.htmlsearch import _seats_from_map
    from core.services.seat import parse_seat_map

    flat = parse_seat_map(DECKER)
    alt = _seats_from_map(DECKER)
    assert [s.number for s in flat] == [s["numero"] for s in alt]
    assert [s.posZ for s in flat] == [s["posZ"] for s in alt] == [0.0, 0.0, 1.0, 1.0]
    assert len(build_decks(DECKER)) == 2


def test_checkout_recheck_uses_the_shared_matcher():
    """It hand-rolled an exact-string compare, which missed "05" vs "5" and fell
    through to "assume locked"."""
    import core.services.checkout as checkout_mod
    assert checkout_mod.find_seat is find_seat


# ─────────────────────────────────────────────────────────────────
# #1 — a reservation identifies its coach
# ─────────────────────────────────────────────────────────────────
def _ls(service_id, hhmm, empresa="CO", date="25/07/2026"):
    return {"servico": f"{service_id}-2026-07-25T{hhmm}-FARE-1",
            "saida": f"{date} {hhmm}", "chegada": f"{date} 13:30", "empresa": empresa}


# Two companies, same route, same minute — the case that was silently mishandled.
CLASH = [_ls("83428", "09:00", "COMPANY A"), _ls("99999", "09:00", "COMPANY B")]


def test_ls_helpers():
    assert ls_service_id(CLASH[0]) == "83428"
    assert ls_departure(CLASH[0]) == "09:00"


def test_service_id_picks_the_right_coach_out_of_a_clash():
    assert select_trip(CLASH, "09:00", "99999")["empresa"] == "COMPANY B"
    assert select_trip(CLASH, "09:00", "83428")["empresa"] == "COMPANY A"


def test_departure_only_matching_is_ambiguous_and_says_so(caplog):
    """Legacy path for records created before service_id existed. It still returns
    something, but the ambiguity is no longer silent."""
    import logging
    with caplog.at_level(logging.WARNING, logger="busbooker"):
        chosen = select_trip(CLASH, "09:00", "")
    assert chosen["empresa"] == "COMPANY A"          # first listed, as before
    assert "no service_id was given" in caplog.text


def test_unknown_service_id_matches_nothing():
    """Must not silently fall back to time — that would reintroduce the bug."""
    assert select_trip(CLASH, "09:00", "00000") is None


def test_service_id_wins_even_if_the_departure_time_differs():
    trips = [_ls("83428", "09:00"), _ls("55555", "14:00")]
    assert ls_departure(select_trip(trips, "09:00", "55555")) == "14:00"


# ── plumbed end to end ───────────────────────────────────────────
def _params(service_id="", departure="09:00") -> RouteParams:
    return RouteParams(origin_id="19052", destination_id="-3", date="2026-07-25",
                       departure=departure, seat="15", date_formatted="25-07-2026",
                       search_url="https://mobifacil.com.br/x", service_id=service_id)


def test_route_params_defaults_service_id_to_empty():
    from core.services.flow import resolve_route_params
    p = resolve_route_params("19052", "-3", "2026-07-25", "09:00", "15")
    assert p.service_id == ""
    p2 = resolve_route_params("19052", "-3", "2026-07-25", "09:00", "15", service_id="83428")
    assert p2.service_id == "83428"


def test_reservation_request_accepts_and_defaults_service_id():
    from core.models.schemas import ReservationRequest
    body = dict(origin_id="19052", destination_id="-3", date="2026-07-25",
                departure="09:00", seat="15")
    assert ReservationRequest(**body).service_id == ""
    assert ReservationRequest(**body, service_id="83428").service_id == "83428"


def test_bus_details_rejects_the_wrong_coach():
    """The DOM fallback can only match on time, so this is the backstop that keeps
    a wrong card from booking silently."""
    data = {"success": True, "details": {"trip": [{
        "departureHour": "09:00", "serviceId": 99999, "fareId": "F", "empresaId": 8}]}}
    assert _parse_bus_details(data, _params(service_id="83428")) is None
    # ...and accepts the right one.
    assert _parse_bus_details(data, _params(service_id="99999"))["serviceId"] == "99999"


def test_bus_details_still_matches_on_time_without_a_service_id():
    data = {"success": True, "details": {"trip": [{
        "departureHour": "09:00", "serviceId": 99999, "fareId": "F", "empresaId": 8}]}}
    assert _parse_bus_details(data, _params(service_id=""))["serviceId"] == "99999"


class _Resp:
    def __init__(self, status, body=""):
        self.status, self._body = status, body

    def text(self):
        return self._body

    def json(self):
        return json.loads(self._body)


class _Page:
    def __init__(self, responses):
        outer = self

        class _R:
            @staticmethod
            def get(url, **kw):
                outer.calls.append(url)
                return responses(url) if callable(responses) else responses
        self.calls = []
        self.request = _R()


def _html(trips):
    payload = {"lsServicos": trips}
    return f'<list-trips :data="{html_mod.escape(json.dumps(payload), quote=True)}">'


def test_resolve_trip_direct_selects_by_service_id():
    captured = {}

    def _responses(url):
        if "BusDetails" in url:
            captured["url"] = url
            return _Resp(503)                 # stop here; we assert on the URL built
        return _Resp(200, _html(CLASH))

    page = _Page(_responses)
    assert _resolve_trip_direct(page, _params(service_id="99999")) is None
    # The BusDetails URL must have been built from COMPANY B's entry.
    assert "99999" in captured["url"]


def test_unknown_service_id_is_reported_as_not_offered():
    page = _Page(_Resp(200, _html(CLASH)))
    with pytest.raises(TripNotOffered) as e:
        _resolve_trip_direct(page, _params(service_id="00000"))
    assert "service 00000" in str(e.value)


def test_reservation_records_the_service_id(monkeypatch, client):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from core.api import routes as routes_mod

    async def _flow(params, rid):
        assert params.service_id == "83428", "service_id must reach the flow"
        return (0, {"arrivalHour": ""})

    monkeypatch.setattr(routes_mod, "run_flow_guarded", _flow)
    monkeypatch.setattr(routes_mod, "schedule_relock", lambda *a, **k: None)

    dep = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Sao_Paulo")) + timedelta(hours=6)
    r = client.post("/reservations", json={
        "id": "svc00001", "origin_id": "19052", "destination_id": "-3",
        "date": dep.strftime("%Y-%m-%d"), "departure": dep.strftime("%H:%M"),
        "seat": "15", "service_id": "83428",
    })
    assert r.status_code == 201, r.text
    assert r.json()["service_id"] == "83428"


def test_relock_carries_the_service_id_forward(monkeypatch):
    """A re-lock 14 minutes later must target the same coach, not just the time."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from core.models.schemas import ReservationRecord, ReservationStatus
    from core.scheduler import jobs as jobs_mod

    now = datetime.now(timezone.utc)
    rec = ReservationRecord(
        id="relock01", origin_id="19052", destination_id="-3", date="2026-07-25",
        departure="09:00", seat="15", service_id="83428",
        status=ReservationStatus.locked, created_at=now, updated_at=now,
        departure_datetime=now + timedelta(hours=6),
    )

    seen = {}

    async def _flow(params, rid, is_relock=False):
        seen["service_id"] = params.service_id
        return (0, None)

    class _Store:
        async def get(self, _):
            return rec

        async def update(self, *a, **k):
            return rec

    monkeypatch.setattr(jobs_mod, "store", _Store())
    monkeypatch.setattr(jobs_mod, "run_flow_guarded", _flow)
    asyncio.run(jobs_mod._relock_job("relock01"))
    assert seen["service_id"] == "83428"
