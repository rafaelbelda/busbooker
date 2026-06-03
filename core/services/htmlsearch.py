"""
Playwright-free path for /search and /seats.

The mobifacil search page is server-rendered: all trip metadata (lsServicos)
is embedded in the <list-trips :data="..."> Vue attribute of the initial HTML
response. No JS execution or browser session is needed to get the trip list.

Key fields available in lsServicos (no BusDetails required):
  saida / chegada  → departure / arrival times
  empresa          → company name
  preco            → price
  classe           → service class
  poltronasLivres  → available seat count
  duration         → human-readable duration ("1h40")
  fareId/fareCode  → needed to construct the BusDetails URL
  empresaId/rutaId → needed for lock payload

BusDetails adds: full seatMap with per-seat availability and hasSecondFloor.
It is only called when individual seat data is needed (/seats endpoint).

Flow for /search:   httpx GET HTML  →  parse lsServicos  (~2s, no browser)
Flow for /seats:    httpx GET HTML  →  BusDetails for target trip  (~3s)
Booking flow:       Playwright only (unchanged) — needs session for LockSeat
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
import urllib.parse
from typing import Optional

import httpx

from ..config import BUS_DETAILS_PATH, settings
from ..utils.logger import log

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": _UA,
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

_JSON_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": _UA,
    "Referer": settings.base_url + "/passagem-de-onibus/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
}


# ─────────────────────────────────────────────────────────────────
# HTML fetch + lsServicos parse
# ─────────────────────────────────────────────────────────────────

def filter_trips_by_date(trips: list[dict], date_yyyymmdd: str) -> list[dict]:
    """Return only trips whose saida date matches date_yyyymmdd (YYYY-MM-DD).

    Mobifacil rolls over to the next day's results when all trips for the
    requested date have already departed. Filtering by date catches this so
    we never surface next-day data to the caller.
    """
    y, m, d = date_yyyymmdd.split("-")
    expected = f"{d}/{m}/{y}"   # mobifacil saida format: "DD/MM/YYYY HH:MM"
    return [t for t in trips if t.get("saida", "").startswith(expected)]


def fetch_lsservicos(
    search_url: str, client: Optional[httpx.Client] = None
) -> list[dict]:
    """GET the search page HTML and return the lsServicos trip list."""
    log.info(f"[htmlsearch] GET {search_url[:90]}")
    t0 = time.monotonic()
    try:
        if client is not None:
            resp = client.get(search_url, headers=_HTML_HEADERS)
            resp.raise_for_status()
        else:
            with httpx.Client(timeout=25, follow_redirects=True) as c:
                resp = c.get(search_url, headers=_HTML_HEADERS)
                resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"search page GET failed: {exc}") from exc

    elapsed = time.monotonic() - t0
    log.info(f"[htmlsearch] page loaded in {elapsed:.1f}s ({len(resp.text)} bytes)")
    return _parse_lsservicos(resp.text)


def _parse_lsservicos(html_content: str) -> list[dict]:
    # The Vue :data="..." attribute contains HTML-encoded JSON.
    # All internal quotes are &quot;, so a [^"]+ match safely captures the value.
    m = re.search(r':data="([^"]+)"', html_content)
    if not m:
        log.warning("[htmlsearch] :data attribute not found — page structure may have changed")
        return []
    try:
        data = json.loads(html_mod.unescape(m.group(1)))
        trips = data.get("lsServicos", [])
        log.info(f"[htmlsearch] {len(trips)} trips in HTML")
        return trips
    except (json.JSONDecodeError, AttributeError) as exc:
        log.warning(f"[htmlsearch] lsServicos parse failed: {exc!r}")
        return []


# ─────────────────────────────────────────────────────────────────
# Seat-map flattening (shared by both search and seats paths)
# ─────────────────────────────────────────────────────────────────

def _posZ_from(seat: dict, fallback: int) -> float:
    """Read the explicit z/posZ floor field, or fall back to the empty-row counter."""
    for k in ("z", "posZ"):
        v = seat.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return float(fallback)


def _seats_from_map(seat_map: list) -> list[dict]:
    """
    Flatten seatMap into TripSeat-compatible dicts.

    Mobifacil's BusDetails encodes seat position in the 2D array structure:
      outer index n → posX (bus depth, 0=front row)
      inner index i → posY (cross-section, corridor marker at i=2 has numero=-99)
    posX and posY are ALWAYS the array indices — the 2D structure is the coordinate
    system. Empty rows [] are floor separators; tracked as posZ so splitFloors()
    can group decks correctly. Non-numeric labels (WC, ES) and -99 are excluded.
    """
    seats: list[dict] = []
    floor = 0
    for n, row in enumerate(seat_map):
        if not isinstance(row, list):
            continue
        if len(row) == 0:
            floor += 1
            continue
        for i, seat in enumerate(row):
            if not isinstance(seat, dict):
                continue
            raw = seat.get("numero", -99)
            if raw == -99 or str(raw) == "-99":
                continue
            num_str = str(raw).strip()
            try:
                num_str = str(int(float(num_str)))  # normalise "5.0" → "5", rejects WC/ES
            except (ValueError, OverflowError):
                continue
            seats.append({
                "numero": num_str,
                "disponivel": bool(seat.get("disponivel", False)),
                "posX": float(n),
                "posY": float(i),
                "posZ": _posZ_from(seat, floor),
            })
    return seats


# ─────────────────────────────────────────────────────────────────
# Trip dict builders
# ─────────────────────────────────────────────────────────────────

def lsservicos_to_search_dict(ls_trip: dict) -> dict:
    """
    Build a TripResult-compatible dict from a single lsServicos entry.
    No BusDetails call needed — lsServicos has all fields required for
    trip listing (price, times, company, class, free-seat count, duration).
    Individual seat maps are NOT included; use /seats for that.
    """
    saida = ls_trip.get("saida", "")      # "02/06/2026 05:00"
    chegada = ls_trip.get("chegada", "")  # "02/06/2026 06:40"
    dep_date = saida.split(" ", 1)[0] if " " in saida else ""
    dep_hour = saida.rsplit(" ", 1)[-1] if " " in saida else saida
    arr_hour = chegada.rsplit(" ", 1)[-1] if " " in chegada else chegada

    # serviceId is the bare number part of servico ("584711-...-FARE-1" → "584711")
    servico = ls_trip.get("servico", "")
    service_id = servico.split("-")[0] if servico else ""

    return {
        "service_id": service_id,
        "departure": dep_hour,
        "arrival": arr_hour,
        "departure_date": dep_date,
        "company": ls_trip.get("empresa", ""),
        "price": str(ls_trip.get("preco", "") or ""),
        "service_class": ls_trip.get("classe", ""),
        "duration": ls_trip.get("duration", ""),
        "available_seats": int(ls_trip.get("poltronasLivres", 0) or 0),
        "has_second_floor": False,  # not available in lsServicos; comes from BusDetails
        "seats": [],
    }


def bus_details_to_search_dict(ls_trip: dict, bus_data: dict) -> Optional[dict]:
    """
    Build a TripResult-compatible dict merging lsServicos metadata with BusDetails
    seat data. Use when individual seat availability is required alongside trip info.
    Price and duration come from lsServicos (BusDetails returns null for price).
    """
    trips = bus_data.get("details", {}).get("trip", [])
    if not trips:
        return None
    t = trips[0]
    sid = str(t.get("serviceId", "")).strip()
    if not sid:
        return None

    saida = ls_trip.get("saida", "")
    chegada = ls_trip.get("chegada", "")
    dep_date = saida.split(" ", 1)[0] if " " in saida else ""
    dep_hour = saida.rsplit(" ", 1)[-1] if " " in saida else saida
    arr_hour = chegada.rsplit(" ", 1)[-1] if " " in chegada else chegada

    return {
        "service_id": sid,
        "departure": str(t.get("departureHour", dep_hour)).strip(),
        "arrival": str(t.get("arrivalHour", arr_hour)).strip(),
        "departure_date": dep_date,
        "company": str(t.get("company", "") or ls_trip.get("empresa", "")),
        "price": str(ls_trip.get("preco", "") or ""),
        "service_class": str(t.get("serviceClass", "") or ls_trip.get("classe", "")),
        "duration": ls_trip.get("duration", ""),
        "available_seats": int(ls_trip.get("poltronasLivres", 0) or 0),
        "has_second_floor": bool(t.get("hasSecondFloor", False)),
        "seats": _seats_from_map(t.get("seatMap", [])),
    }


# ─────────────────────────────────────────────────────────────────
# BusDetails URL construction
# ─────────────────────────────────────────────────────────────────

def build_bus_details_url(ls_trip: dict, date: str) -> str:
    """
    Construct the BusDetails URL from a lsServicos trip entry.
    All parameters come from the HTML — identical to what the browser sends
    when a trip card is clicked.
    """
    saida = ls_trip.get("saida", "")      # "02/06/2026 05:00"
    chegada = ls_trip.get("chegada", "")  # "02/06/2026 06:40"
    dep_hour = saida.rsplit(" ", 1)[-1] if " " in saida else ""
    arr_hour = chegada.rsplit(" ", 1)[-1] if " " in chegada else ""

    params: dict[str, str] = {
        "hasConnection":              str(ls_trip.get("hasConnection", False)).lower(),
        "offerId":                    str(ls_trip.get("offerId") or ""),
        "offerBundle":                "",
        "isDistribusion":             str(ls_trip.get("isDistribusion", True)).lower(),
        "multipleFares":              str(ls_trip.get("multipleFares", False)).lower(),
        "origin":                     str(ls_trip.get("originId", "")),
        "originIdDistribusion":       str(ls_trip.get("originIdDistribusion") or ls_trip.get("originId", "")),
        "originName":                 ls_trip.get("originName", ""),
        "destinationIdDistribusion":  str(ls_trip.get("destinationIdDistribusion") or ls_trip.get("destinationId", "")),
        "destination":                str(ls_trip.get("destinationId", "")),
        "destinationName":            ls_trip.get("destinationName", ""),
        "arrivalStation":             str(ls_trip.get("arrivalStation") or ls_trip.get("destinationId", "")),
        "fareId":                     ls_trip.get("fareId", ""),
        "fareCode":                   ls_trip.get("fareCode", ""),
        "fareCodeFirstTrip":          str(ls_trip.get("fareCodeFirstTrip") or ""),
        "fareCodeSecondTrip":         str(ls_trip.get("fareCodeSecondTrip") or ""),
        "group":                      ls_trip.get("grupo", "TOTAL_BUS"),
        "service":                    ls_trip.get("servico", ""),
        "preco":                      str(ls_trip.get("preco", "")),
        "date":                       date,
        "returnDate":                 "",
        "step":                       "1",
        "isStudent":                  "false",
        "isPCD":                      "false",
        "class":                      ls_trip.get("classe", ""),
        "arr":                        ls_trip.get("arr", ""),
        "isAjax":                     "true",
        "company":                    ls_trip.get("empresa", ""),
        "saida":                      saida,
        "rutaId":                     str(ls_trip.get("rutaId", 0)),
        "empresaId":                  str(ls_trip.get("empresaId", "")),
        "connection":                 "",
        "raceDate":                   date,
        "chegada":                    chegada,
        "departureHour":              dep_hour,
        "arrivalHour":                arr_hour,
        "position":                   "0",
        "isMobioferta":               str(ls_trip.get("isMobioferta", False)).lower(),
    }
    return settings.base_url + BUS_DETAILS_PATH + "?" + urllib.parse.urlencode(params)


# ─────────────────────────────────────────────────────────────────
# BusDetails fetch
# ─────────────────────────────────────────────────────────────────

def fetch_bus_details(
    url: str, client: Optional[httpx.Client] = None
) -> Optional[dict]:
    """httpx GET a BusDetails URL, return the parsed JSON body or None on failure."""
    try:
        if client is not None:
            resp = client.get(url, headers=_JSON_HEADERS)
        else:
            with httpx.Client(timeout=15, follow_redirects=True) as c:
                resp = c.get(url, headers=_JSON_HEADERS)
        if not resp.is_success:
            log.warning(f"[htmlsearch] BusDetails HTTP {resp.status_code}")
            return None
        data = resp.json()
        if not data.get("success"):
            log.warning(f"[htmlsearch] BusDetails success=false: {str(data)[:120]}")
            return None
        return data
    except Exception as exc:
        log.warning(f"[htmlsearch] BusDetails error: {exc!r}")
        return None
