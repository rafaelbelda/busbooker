"""
Coverage for the pure logic that carries the most provider-format risk.

None of this needs a browser or a network call — it is parsing and payload
construction — yet it is where a silent mobifacil format change would break real
bookings. Fixtures mirror the shapes seen in production logs, notably the real
seat object: ``{"disponivel", "numero", "idoso", "label", "x", "y", "z"}`` with
the coordinates as *strings*.

These also pin the behaviour that #24 (consolidate the three seat-map parsers)
and #1 (trip identity) will refactor.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import json
from datetime import datetime, timezone

import pytest

from core.models.schemas import ReservationRecord, ReservationStatus, RouteParams
from core.services.htmlsearch import (
    _parse_lsservicos,
    _seats_from_map,
    build_seat_decks,
    filter_trips_by_date,
    lsservicos_to_search_dict,
)
from core.services.seat import (
    _build_lock_payload,
    _coerce_seats_with_price,
    _info_connection,
    _resolve_seat_label,
    check_seat_availability,
    parse_seat_map,
    seat_is_locked,
)
from core.services.trip import _parse_bus_details


def _seat(numero, disponivel=True, idoso=False):
    """A seat exactly as BusDetails returns it (coordinates are strings)."""
    return {"disponivel": disponivel, "numero": numero, "idoso": idoso,
            "label": str(numero), "x": "1", "y": "0", "z": "0"}


AISLE = {"numero": -99, "disponivel": False}

# A small coach: two rows, aisle in the middle column, plus a WC marker.
SEAT_MAP = [
    [_seat("01", False), _seat("02"), AISLE, _seat("03"), _seat("04")],
    [_seat("05"), _seat("06", idoso=True), AISLE, _seat("07"), {"numero": "WC"}],
]


def _params(seat="5", departure="09:00") -> RouteParams:
    return RouteParams(origin_id="19052", destination_id="-3", date="2026-07-25",
                       departure=departure, seat=seat, date_formatted="25-07-2026",
                       search_url="https://mobifacil.com.br/x")


# ─────────────────────────────────────────────────────────────────
# Seat map flattening
# ─────────────────────────────────────────────────────────────────
def test_parse_seat_map_excludes_aisles_and_non_numeric_labels():
    seats = parse_seat_map(SEAT_MAP)
    assert [s.number for s in seats] == ["1", "2", "3", "4", "5", "6", "7"]
    assert not seats[0].available and seats[1].available


def test_parse_seat_map_normalises_zero_padding_and_floats():
    assert [s.number for s in parse_seat_map([[_seat("05"), _seat("6.0")]])] == ["5", "6"]


def test_parse_seat_map_uses_array_indices_as_coordinates():
    """The 2D structure IS the coordinate system; the per-seat x/y must not be
    trusted (they are "1"/"0" on every seat in production data)."""
    seats = parse_seat_map(SEAT_MAP)
    assert (seats[0].posX, seats[0].posY) == (0.0, 0.0)
    assert (seats[2].posX, seats[2].posY) == (0.0, 3.0)   # row 0, col 3
    assert seats[4].posX == 1.0                            # second row


def test_empty_rows_separate_decks():
    m = [[_seat("01")], [], [_seat("02")]]
    assert [s.posZ for s in parse_seat_map(m)] == [0.0, 1.0]
    decks = build_seat_decks(m)
    assert len(decks) == 2
    # Mobifacil shows the segment AFTER the divider first (Primeiro Piso).
    assert decks[0]["rows"][0][0]["number"] == "02"


def test_build_seat_decks_keeps_grid_slots_for_alignment():
    """Unlike the flat list, the deck grid must retain aisles/markers so columns
    stay aligned in the picker."""
    rows = build_seat_decks(SEAT_MAP)[0]["rows"]
    assert [c["kind"] for c in rows[0]] == ["seat", "seat", "aisle", "seat", "seat"]
    assert rows[1][4]["kind"] == "bathroom"
    assert rows[1][1]["idoso"] is True


def test_flat_and_deck_parsers_agree_on_bookable_seats():
    """Two independent implementations of the same idea (#24 will merge them)."""
    flat = {s.number for s in parse_seat_map(SEAT_MAP)}
    alt = {s["numero"] for s in _seats_from_map(SEAT_MAP)}
    assert flat == alt


# ─────────────────────────────────────────────────────────────────
# Availability
# ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("requested", ["2", "02", " 2 "])
def test_availability_matches_padded_and_unpadded(requested):
    assert check_seat_availability(SEAT_MAP, _params(seat=requested)) is True


def test_taken_seat_is_unavailable():
    assert check_seat_availability(SEAT_MAP, _params(seat="1")) is False


def test_idoso_seat_is_treated_as_unavailable():
    """Priority seats are attendance-only; a LockSeat POST would be refused."""
    assert check_seat_availability(SEAT_MAP, _params(seat="6")) is False


def test_unknown_seat_is_unavailable():
    assert check_seat_availability(SEAT_MAP, _params(seat="99")) is False


def test_seat_is_locked_detects_our_own_hold():
    assert seat_is_locked(SEAT_MAP, _params(seat="1")) is True     # disponivel=False
    assert seat_is_locked(SEAT_MAP, _params(seat="2")) is False    # still free
    assert seat_is_locked(SEAT_MAP, _params(seat="6")) is False    # idoso, never ours


# ─────────────────────────────────────────────────────────────────
# LockSeat payload
# ─────────────────────────────────────────────────────────────────
def _trip(**kw):
    base = {
        "service": "83428-2026-07-25T09:00-FARE-1", "empresaId": "8",
        "fareId": "FARE-1", "fareCode": "FARE-1", "seatMap": SEAT_MAP,
        "seatsWithPrice": SEAT_MAP, "company": "VIACAO PIRACICABANA SA",
        "arrival": "25/07/2026 13:30:00", "departure": "25/07/2026 09:00:00",
        "originId": "19052", "destinationId": "21787", "serviceClass": "EXECUTIVO",
    }
    base.update(kw)
    return base


def test_lock_payload_sends_the_raw_zero_padded_seat_label():
    """Mobifacil matches on seatMap's `numero` ("05"), not our normalised "5"."""
    assert _build_lock_payload(_trip(), _params(seat="5"))["seat"] == "05"


def test_resolve_seat_label_falls_back_to_the_request():
    assert _resolve_seat_label(_trip(), "99") == "99"


def test_seats_with_price_is_real_json_not_a_python_repr():
    """str([...]) yields single quotes — invalid JSON the server rejects."""
    out = _coerce_seats_with_price(_trip())
    assert "'" not in out
    assert json.loads(out)[0][0]["numero"] == "01"


def test_empty_seats_with_price_becomes_an_empty_string():
    """So the payload's drop-empties filter actually removes it."""
    assert _coerce_seats_with_price({"seatsWithPrice": []}) == ""
    assert "seatMap" not in _build_lock_payload(_trip(seatsWithPrice=[]), _params())


def test_info_connection_is_always_json_parseable():
    """The server JSON.parses this field; "" or absent makes it parse undefined."""
    assert _info_connection({}) == "null"
    assert _info_connection({"objConnection": None}) == "null"
    assert json.loads(_info_connection({"objConnection": {"a": 1}})) == {"a": 1}


def test_lock_payload_drops_empty_values_but_keeps_info_connection():
    payload = _build_lock_payload(_trip(rutaId="", originUf=""), _params())
    assert "rutaId" not in payload and "originUf" not in payload
    assert payload["infoConnection"] == "null"


# ─────────────────────────────────────────────────────────────────
# BusDetails / lsServicos parsing
# ─────────────────────────────────────────────────────────────────
def test_parse_bus_details_rejects_a_mismatched_departure():
    data = {"success": True, "details": {"trip": [{"departureHour": "11:00"}]}}
    assert _parse_bus_details(data, _params(departure="09:00")) is None


def test_parse_bus_details_prefers_resolved_terminal_ids():
    """BusDetails carries the real terminal ids; the search meta-origin (-3) must
    not leak into the lock payload."""
    data = {"success": True, "details": {"trip": [{
        "departureHour": "09:00", "serviceId": 83428, "fareId": "X-FARE-1",
        "empresaId": 8, "origin": "19052", "destination": "21787",
    }]}}
    trip = _parse_bus_details(data, _params(departure="09:00"))
    assert trip["originId"] == "19052" and trip["destinationId"] == "21787"
    assert trip["fareCode"] == "FARE-1"


def test_parse_bus_details_lowercases_is_distribusion():
    """str(True) == "True", which the server rejects."""
    data = {"success": True, "details": {"trip": [
        {"departureHour": "09:00", "isDistribusion": True}]}}
    assert _parse_bus_details(data, _params(departure="09:00"))["isDistribusion"] == "true"


def test_filter_trips_by_date_catches_the_provider_rolling_over():
    trips = [{"saida": "25/07/2026 09:00"}, {"saida": "26/07/2026 07:00"}]
    assert len(filter_trips_by_date(trips, "2026-07-25")) == 1
    assert filter_trips_by_date(trips, "2026-07-27") == []


def test_parse_lsservicos_reads_the_html_encoded_vue_attribute():
    payload = {"lsServicos": [{"saida": "25/07/2026 09:00", "empresa": "X"}]}
    html = f'<list-trips :data="{html_mod.escape(json.dumps(payload), quote=True)}">'
    assert _parse_lsservicos(html)[0]["empresa"] == "X"


def test_parse_lsservicos_returns_empty_on_a_changed_page():
    assert _parse_lsservicos("<html>nothing here</html>") == []


def test_lsservicos_to_search_dict_extracts_the_service_id():
    out = lsservicos_to_search_dict({
        "saida": "25/07/2026 09:00", "chegada": "25/07/2026 13:30",
        "servico": "83428-2026-07-25T09:00-FARE-1", "poltronasLivres": 20,
    })
    assert out["service_id"] == "83428"
    assert (out["departure"], out["arrival"]) == ("09:00", "13:30")
    assert out["available_seats"] == 20


# ─────────────────────────────────────────────────────────────────
# Search-URL validation (SSRF surface)
# ─────────────────────────────────────────────────────────────────
def test_search_url_accepts_a_valid_mobifacil_link():
    from core.api.routes import _parse_search_url
    out = _parse_search_url(
        "https://mobifacil.com.br/passagem-de-onibus/x?origin=1&destination=2&date=25-07-2026")
    assert out["date"] == "2026-07-25"


@pytest.mark.parametrize("url", [
    "https://evil.com/passagem-de-onibus/?origin=1&destination=2&date=25-07-2026",
    "https://mobifacil.com.br.evil.com/passagem-de-onibus/?origin=1&destination=2&date=25-07-2026",
    "https://mobifacil.com.br/other-path/?origin=1&destination=2&date=25-07-2026",
    "https://mobifacil.com.br/passagem-de-onibus/?origin=1&destination=2",
    "https://mobifacil.com.br/passagem-de-onibus/?origin=1&destination=2&date=2026-07-25",
])
def test_search_url_rejects_bad_input(url):
    from fastapi import HTTPException

    from core.api.routes import _parse_search_url
    with pytest.raises(HTTPException) as e:
        _parse_search_url(url)
    assert e.value.status_code == 422


# ─────────────────────────────────────────────────────────────────
# Store transitions
# ─────────────────────────────────────────────────────────────────
def _rec(rid="rec00001", **kw):
    now = datetime.now(timezone.utc)
    base = dict(id=rid, origin_id="19052", destination_id="-3", date="2026-07-25",
                departure="09:00", seat="15", created_at=now, updated_at=now)
    base.update(kw)
    return ReservationRecord(**base)


def test_only_if_active_refuses_to_resurrect_a_cancelled_record():
    """A long flow must never revive a reservation cancelled while it ran."""
    from core.state import ReservationStore

    async def go():
        store = ReservationStore()
        await store.add(_rec(status=ReservationStatus.cancelled))
        assert await store.update("rec00001", only_if_active=True,
                                  status=ReservationStatus.locked) is None
        assert (await store.get("rec00001")).status is ReservationStatus.cancelled
        # Without the flag the same update goes through (admin paths rely on it).
        assert await store.update("rec00001", status=ReservationStatus.locked) is not None

    asyncio.run(go())


def test_update_on_a_missing_record_returns_none():
    from core.state import ReservationStore
    asyncio.run(_missing())


async def _missing():
    from core.state import ReservationStore
    store = ReservationStore()
    assert await store.update("nope", status=ReservationStatus.locked) is None
    assert await store.delete("nope") is False


def test_is_expired_is_computed_from_departure():
    from datetime import timedelta
    assert _rec().is_expired is False        # no departure yet
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert _rec(departure_datetime=past).is_expired is True
