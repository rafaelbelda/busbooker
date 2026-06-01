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

def fetch_lsservicos(search_url: str) -> list[dict]:
    """GET the search page HTML and return the lsServicos trip list."""
    log.info(f"[htmlsearch] GET {search_url[:90]}")
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=25, follow_redirects=True) as client:
            resp = client.get(search_url, headers=_HTML_HEADERS)
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

def _seats_from_map(seat_map: list) -> list[dict]:
    """
    Flatten seatMap into TripSeat-compatible dicts.
    BusDetails uses x/y for grid position; posX/posY are used as output names
    to match the TripSeat schema. Both field names are checked on input.
    """
    seats: list[dict] = []
    for row in seat_map:
        if not isinstance(row, list):
            continue
        for seat in row:
            if not isinstance(seat, dict):
                continue
            numero = str(seat.get("numero", "")).strip()
            if not numero or numero == "-99":
                continue
            try:
                pos_x = float(seat.get("posX") or seat.get("x") or 0)
                pos_y = float(seat.get("posY") or seat.get("y") or 0)
            except (TypeError, ValueError):
                pos_x = pos_y = 0.0
            seats.append({
                "numero": numero,
                "disponivel": bool(seat.get("disponivel", False)),
                "posX": pos_x,
                "posY": pos_y,
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
    dep_hour = saida.rsplit(" ", 1)[-1] if " " in saida else saida
    arr_hour = chegada.rsplit(" ", 1)[-1] if " " in chegada else chegada

    # serviceId is the bare number part of servico ("584711-...-FARE-1" → "584711")
    servico = ls_trip.get("servico", "")
    service_id = servico.split("-")[0] if servico else ""

    return {
        "service_id": service_id,
        "departure": dep_hour,
        "arrival": arr_hour,
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
    dep_hour = saida.rsplit(" ", 1)[-1] if " " in saida else saida
    arr_hour = chegada.rsplit(" ", 1)[-1] if " " in chegada else chegada

    return {
        "service_id": sid,
        "departure": str(t.get("departureHour", dep_hour)).strip(),
        "arrival": str(t.get("arrivalHour", arr_hour)).strip(),
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

def fetch_bus_details(url: str) -> Optional[dict]:
    """httpx GET a BusDetails URL, return the parsed JSON body or None on failure."""
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(url, headers=_JSON_HEADERS)
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
