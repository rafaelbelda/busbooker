"""
Playwright-free path for /search and /seats.

The mobifacil search page is server-rendered: all trip metadata (lsServicos)
is embedded in the <list-trips :data="..."> attribute of the initial HTML
response. No JS execution or browser session needed to get the trip list.

BusDetails (seat maps) are only loaded on card click in the real site, but
the URL is fully constructable from lsServicos fields. We call it directly
with httpx once per trip, using the same parameters the browser would send.

Flow:
  1. httpx GET search URL  →  parse lsServicos from HTML   (~2s, no browser)
  2. For each trip: construct + call BusDetails URL         (~1s each, parallel-safe)
  3. Merge: price from lsServicos, seatMap from BusDetails
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
    # The Vue component attribute :data="..." contains HTML-encoded JSON.
    # All internal " are &quot; so a simple [^"]+ match works safely.
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
# BusDetails URL construction
# ─────────────────────────────────────────────────────────────────

def build_bus_details_url(ls_trip: dict, date: str) -> str:
    """
    Construct the BusDetails URL from a lsServicos trip entry.
    All parameters come from the HTML — the same values the browser sends
    when a trip card is clicked.
    """
    saida = ls_trip.get("saida", "")      # "02/06/2026 05:00"
    chegada = ls_trip.get("chegada", "")  # "02/06/2026 06:40"
    dep_hour = saida.rsplit(" ", 1)[-1] if " " in saida else ""
    arr_hour = chegada.rsplit(" ", 1)[-1] if " " in chegada else ""

    params: dict[str, str] = {
        "hasConnection":           str(ls_trip.get("hasConnection", False)).lower(),
        "offerId":                 ls_trip.get("offerId") or "",
        "offerBundle":             "",
        "isDistribusion":          str(ls_trip.get("isDistribusion", True)).lower(),
        "multipleFares":           str(ls_trip.get("multipleFares", False)).lower(),
        "origin":                  str(ls_trip.get("originId", "")),
        "originIdDistribusion":    str(ls_trip.get("originIdDistribusion") or ls_trip.get("originId", "")),
        "originName":              ls_trip.get("originName", ""),
        "destinationIdDistribusion": str(ls_trip.get("destinationIdDistribusion") or ls_trip.get("destinationId", "")),
        "destination":             str(ls_trip.get("destinationId", "")),
        "destinationName":         ls_trip.get("destinationName", ""),
        "arrivalStation":          str(ls_trip.get("arrivalStation") or ls_trip.get("destinationId", "")),
        "fareId":                  ls_trip.get("fareId", ""),
        "fareCode":                ls_trip.get("fareCode", ""),
        "fareCodeFirstTrip":       ls_trip.get("fareCodeFirstTrip") or "",
        "fareCodeSecondTrip":      ls_trip.get("fareCodeSecondTrip") or "",
        "group":                   ls_trip.get("grupo", "TOTAL_BUS"),
        "service":                 ls_trip.get("servico", ""),
        "preco":                   str(ls_trip.get("preco", "")),
        "date":                    date,
        "returnDate":              "",
        "step":                    "1",
        "isStudent":               "false",
        "isPCD":                   "false",
        "class":                   ls_trip.get("classe", ""),
        "arr":                     ls_trip.get("arr", ""),
        "isAjax":                  "true",
        "company":                 ls_trip.get("empresa", ""),
        "saida":                   saida,
        "rutaId":                  str(ls_trip.get("rutaId", 0)),
        "empresaId":               str(ls_trip.get("empresaId", "")),
        "connection":              "",
        "raceDate":                date,
        "chegada":                 chegada,
        "departureHour":           dep_hour,
        "arrivalHour":             arr_hour,
        "position":                "0",
        "isMobioferta":            str(ls_trip.get("isMobioferta", False)).lower(),
    }
    return settings.base_url + BUS_DETAILS_PATH + "?" + urllib.parse.urlencode(params)


# ─────────────────────────────────────────────────────────────────
# BusDetails fetch
# ─────────────────────────────────────────────────────────────────

def fetch_bus_details(url: str) -> Optional[dict]:
    """httpx GET a BusDetails URL, return the JSON body or None on failure."""
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


# ─────────────────────────────────────────────────────────────────
# Trip dict builder (merges lsServicos price with BusDetails seats)
# ─────────────────────────────────────────────────────────────────

def _seats_from_map(seat_map: list) -> list[dict]:
    seats = []
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
                pos_x = float(seat.get("posX", seat.get("x", 0)) or 0)
                pos_y = float(seat.get("posY", seat.get("y", 0)) or 0)
            except (TypeError, ValueError):
                pos_x = pos_y = 0.0
            seats.append({
                "numero": numero,
                "disponivel": bool(seat.get("disponivel", False)),
                "posX": pos_x,
                "posY": pos_y,
            })
    return seats


def bus_details_to_search_dict(ls_trip: dict, bus_data: dict) -> Optional[dict]:
    """
    Merge lsServicos metadata with BusDetails seat data.
    Price comes from lsServicos (BusDetails returns null for price).
    seatMap comes from BusDetails (not in lsServicos).
    """
    trips = bus_data.get("details", {}).get("trip", [])
    if not trips:
        return None
    t = trips[0]
    sid = str(t.get("serviceId", "")).strip()
    if not sid:
        return None
    return {
        "service_id": sid,
        "departure":     str(t.get("departureHour", "")).strip(),
        "arrival":       str(t.get("arrivalHour", "")).strip(),
        "company":       str(t.get("company", "") or ls_trip.get("empresa", "")),
        "price":         str(ls_trip.get("preco", "") or ""),
        "service_class": str(t.get("serviceClass", "") or ls_trip.get("classe", "")),
        "seats":         _seats_from_map(t.get("seatMap", [])),
    }


def bus_details_to_trip_dict(ls_trip: dict, bus_data: dict) -> Optional[dict]:
    """
    Build a full trip dict for the booking flow (same shape as resolve_trip output).
    Only needed if we want to replace resolve_trip for /seats.
    """
    trips = bus_data.get("details", {}).get("trip", [])
    if not trips:
        return None
    t = trips[0]
    sid = str(t.get("serviceId", "") or "")
    fare_id = str(t.get("fareId", "") or ls_trip.get("fareId", ""))
    fare_code = fare_id.split("-", 1)[1] if "-" in fare_id else "FARE-1"
    return {
        "serviceId":     sid,
        "fareId":        fare_id,
        "fareCode":      fare_code,
        "empresaId":     str(t.get("empresaId", "") or ls_trip.get("empresaId", "")),
        "departureHour": str(t.get("departureHour", "")),
        "arrivalHour":   str(t.get("arrivalHour", "")),
        "service":       str(ls_trip.get("servico", "")),
        "seatMap":       t.get("seatMap", []),
        "preco":         str(ls_trip.get("preco", "") or ""),
        "company":       str(t.get("company", "") or ls_trip.get("empresa", "")),
        "originId":      str(ls_trip.get("originId", "")),
        "destinationId": str(ls_trip.get("destinationId", "")),
        "group":         str(ls_trip.get("grupo", "TOTAL_BUS")),
        "raceDate":      str(t.get("raceDate", "") or ls_trip.get("dataCorrida", "")),
        "rutaId":        str(ls_trip.get("rutaId", "")),
        "serviceClass":  str(t.get("serviceClass", "") or ls_trip.get("classe", "")),
        "originUf":      str(t.get("originUf", "")),
        "stepNumber":    str(t.get("stepNumber", "1")),
        "offerId":       str(t.get("offerId", "") or ""),
        "connectionId":  str(t.get("connectionId", "") or ""),
        "isDistribusion": str(ls_trip.get("isDistribusion", True)),
        "seatsWithPrice": t.get("seatsWithPrice", ""),
        "departure":     str(t.get("departure", t.get("departureHour", ""))),
        "arrival":       str(t.get("arrival", t.get("arrivalHour", ""))),
    }
