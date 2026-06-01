"""
Steps 1-2 of the flow: load the search page, resolve the target trip by
intercepting the BusDetails XHR, and parse it into the trip dict consumed by
the seat/checkout steps.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional, Tuple

from playwright.sync_api import Page, Response

from ..config import BUS_DETAILS_PATH, settings
from ..models.schemas import RouteParams
from ..utils.logger import log
from .browser import (
    TelemetryWatcher,
    check_detection,
    jitter,
    retry,
    stochastic_idle,
)


# ─────────────────────────────────────────────────────────────────
# Seat-map debug helper
# ─────────────────────────────────────────────────────────────────
def debug_seat_map_structure(seat_map: list) -> None:
    """Log the structure of seatMap to understand coordinate fields."""
    log.info("[seatmap] debugging seatMap structure:")
    for i, row in enumerate(seat_map[:3]):  # First 3 rows
        if isinstance(row, list) and len(row) > 0:
            seat = row[0]
            log.info(f"[seatmap] row {i}, first seat keys: {list(seat.keys())}")
            log.info(f"[seatmap] row {i}, first seat data: {json.dumps(seat)[:300]}")
            break


# ─────────────────────────────────────────────────────────────────
# Step 1 — Search page
# ─────────────────────────────────────────────────────────────────
def open_search_page(page: Page, telemetry: TelemetryWatcher, params: RouteParams) -> None:
    log.info("[step 1] loading search page")

    def _load() -> None:
        page.goto(params.search_url, wait_until="networkidle", timeout=55_000)
        if "mobifacil" not in page.url:
            raise RuntimeError(f"Unexpected redirect: {page.url}")
        check_detection(page, "search_load")
        names = [c["name"] for c in page.context.cookies()]
        dw = [k for k in names if k.startswith("dw") or k == "sid"]
        if not dw:
            raise RuntimeError("Session cookies absent")
        log.info(f"[step 1] session cookies: {dw}")
        try:
            page.wait_for_selector(".listTripsCard", timeout=15000)
            page.wait_for_timeout(2000)
            log.info("[step 1] trip list rendered")
        except Exception as exc:  # FIX (bug 2): no bare except
            raise RuntimeError("Trip list not rendered") from exc

    retry(_load, "open_search_page")
    stochastic_idle(page, "post_search_load")
    jitter(600, 1400)


# ─────────────────────────────────────────────────────────────────
# Step 2 — Trip resolution
# ─────────────────────────────────────────────────────────────────
def _parse_bus_details(data: dict, params: RouteParams) -> Optional[dict]:
    if not data.get("success"):
        return None
    trips = data.get("details", {}).get("trip", [])
    if not trips:
        return None
    trip = trips[0]
    dep = trip.get("departureHour", "")
    if params.departure not in dep:
        return None

    # FIX (bug 3): the original used trip["serviceId"]/["fareId"]/["empresaId"]
    # — a KeyError there bubbled into the response handler and was swallowed as a
    # vague "intercept parse error", so the trip was never captured. Use .get()
    # with safe fallbacks and warn loudly when required fields are missing.
    sid_raw = trip.get("serviceId")
    fare_id_raw = trip.get("fareId")
    empresa_raw = trip.get("empresaId")
    missing = [
        name
        for name, val in (
            ("serviceId", sid_raw),
            ("fareId", fare_id_raw),
            ("empresaId", empresa_raw),
        )
        if val in (None, "")
    ]
    if missing:
        log.warning(f"[step 2] BusDetails missing fields {missing} — using fallbacks")

    sid = str(sid_raw) if sid_raw is not None else ""
    fare_id = str(fare_id_raw) if fare_id_raw is not None else ""
    fare_code = fare_id.split("-", 1)[1] if "-" in fare_id else "FARE-1"
    empresa_id = str(empresa_raw) if empresa_raw is not None else ""

    return {
        "serviceId": sid,
        "fareId": fare_id,
        "fareCode": fare_code,
        "empresaId": empresa_id,
        "departureHour": dep,
        "arrivalHour": trip.get("arrivalHour", "11:30"),
        "service": f"{sid}-{params.date}T{params.departure}-{fare_code}",
        "seatMap": trip.get("seatMap", []),
        "preco": str(trip.get("price") or "130.55"),
        "company": trip.get("company", ""),
        "originId": str(trip.get("originId", params.origin_id)),
        "destinationId": str(trip.get("destinationId", params.destination_id)),
        "group": trip.get("group", "TOTAL_BUS"),
        "raceDate": trip.get("raceDate", params.date),
        "rutaId": str(trip.get("rutaId", "")),
        "serviceClass": trip.get("serviceClass", ""),
        "originUf": trip.get("originUf", ""),
        "stepNumber": str(trip.get("stepNumber", "1")),
        "offerId": str(trip.get("offerId", "")),
        "connectionId": str(trip.get("connectionId", "")),
        "isDistribusion": str(trip.get("isDistribusion", "true")),
        "seatsWithPrice": trip.get("seatsWithPrice", ""),
        "departure": trip.get("departure", dep),
        "arrival": trip.get("arrival", trip.get("arrivalHour", "11:30")),
    }


def _attach_bus_details_listener(
    page: Page, params: RouteParams
) -> Tuple[dict, Callable[[], None]]:
    captured: dict = {}

    def on_response(response: Response) -> None:
        if BUS_DETAILS_PATH not in response.url or captured:
            return
        try:
            data = response.json()
            result = _parse_bus_details(data, params)
            if result:
                captured.update(result)
                log.info(f"[step 2] intercepted BusDetails — serviceId={result['serviceId']}")
                debug_seat_map_structure(result["seatMap"])
        except Exception as exc:
            log.warning(f"[step 2] intercept parse error: {exc!r}")

    page.on("response", on_response)
    return captured, lambda: page.remove_listener("response", on_response)


def _click_trip_card(page: Page, params: RouteParams) -> bool:
    stochastic_idle(page, "pre_trip_click")
    cards = page.locator(".listTripsCard")
    count = cards.count()
    log.info(f"[step 2] found {count} trip cards")

    for i in range(count):
        card = cards.nth(i)
        try:
            cls = card.get_attribute("class") or ""
            if "soldOut" in cls:
                continue
            hour_el = card.locator(".listTripsCard__departureHour strong")
            if not hour_el.count():
                continue
            hour = hour_el.inner_text().strip()
            if hour != params.departure:
                continue

            log.info(f"[step 2] clicking trip card at index {i} ({hour})")
            try:
                card.click(timeout=5000, force=True)
            except Exception:  # FIX (bug 2)
                btn = card.locator("button, .btn-select, [class*='select']").first
                btn.click(timeout=3000, force=True)

            stochastic_idle(page, "post_trip_click")
            return True
        except Exception as e:
            log.debug(f"[step 2] card {i} error: {e}")
            continue
    return False


def _extract_bus_url(page: Page, params: RouteParams) -> Optional[str]:
    cards = page.locator(".listTripsCard")
    for i in range(cards.count()):
        card = cards.nth(i)
        try:
            cls = card.get_attribute("class") or ""
            if "soldOut" in cls:
                continue
            hour_el = card.locator(".listTripsCard__departureHour strong")
            if not hour_el.count():
                continue
            hour = hour_el.inner_text().strip()
            if hour != params.departure:
                continue
            url = card.get_attribute("data-urlbusdetails")
            if url:
                return url
        except Exception:  # FIX (bug 2)
            continue
    return None


def _wait_for_intercept(captured: dict, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if captured:
            return True
        time.sleep(0.35)
    return False


def _navigate_via_url(page: Page, params: RouteParams) -> None:
    """Fallback navigation when the trip card click fails."""
    bus_url = _extract_bus_url(page, params)
    if not bus_url:
        raise RuntimeError("Trip card click + URL fallback both failed")
    log.info(f"[step 2] navigating to: {bus_url}")
    page.goto(bus_url, wait_until="networkidle", timeout=30_000)
    page.wait_for_timeout(3000)
    for _ in range(5):
        try:
            page.wait_for_selector(
                "canvas, svg, [class*='seat'], [class*='poltrona']", timeout=5000
            )
            log.info("[step 2] seat map rendered")
            break
        except Exception:  # FIX (bug 2)
            page.mouse.wheel(0, 200)
            jitter(500, 1000)
    stochastic_idle(page, "post_bus_details_nav")


def resolve_trip(page: Page, params: RouteParams) -> dict:
    log.info("[step 2] resolving trip (intercept-only)")

    for attempt, label in enumerate(["first attempt", "reload retry"]):
        if attempt == 1:
            log.warning("[step 2] reloading page for retry")
            page.reload(wait_until="networkidle", timeout=55_000)
            check_detection(page, "reload")
            jitter(1500, 2500)

        captured, remove = _attach_bus_details_listener(page, params)
        stochastic_idle(page, "pre_trip_click")
        clicked = _click_trip_card(page, params)

        if not clicked:
            log.warning(f"[step 2] {label}: click failed — trying URL")
            try:
                _navigate_via_url(page, params)
            except RuntimeError:
                remove()
                if attempt == 0:
                    continue
                raise
        else:
            stochastic_idle(page, "post_trip_click")

        got = _wait_for_intercept(captured, timeout=15.0)
        remove()

        if got:
            log.info(f"[step 2] trip resolved via intercept on {label}")
            return captured

        log.warning(f"[step 2] {label}: intercept not captured")

    raise RuntimeError("Trip resolution failed after retry")


# ─────────────────────────────────────────────────────────────────
# URL search — resolve ALL trips for a route/date (used by POST /search)
# ─────────────────────────────────────────────────────────────────
def _seats_from_map(seat_map: list) -> list[dict]:
    """Flatten a seatMap into [{numero, disponivel, posX, posY}, ...]."""
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
                pos_x = float(seat.get("posX", 0) or 0)
                pos_y = float(seat.get("posY", 0) or 0)
            except (TypeError, ValueError):
                pos_x = pos_y = 0.0
            seats.append(
                {
                    "numero": numero,
                    "disponivel": bool(seat.get("disponivel", False)),
                    "posX": pos_x,
                    "posY": pos_y,
                }
            )
    return seats


def _trip_to_search_dict(t: dict) -> dict:
    sid = str(t.get("serviceId", "")).strip()
    return {
        "service_id": sid,
        "departure": str(t.get("departureHour", "")).strip(),
        "arrival": str(t.get("arrivalHour", "")).strip(),
        "company": str(t.get("company", "")),
        "price": str(t.get("price") or ""),
        "service_class": str(t.get("serviceClass", "")),
        "seats": _seats_from_map(t.get("seatMap", [])),
    }


def _parse_all_trips(data: dict) -> list[dict]:
    """Extract every trip in a BusDetails response (no departure filtering)."""
    if not data.get("success"):
        return []
    out: list[dict] = []
    for t in data.get("details", {}).get("trip", []):
        if isinstance(t, dict) and t.get("serviceId") is not None:
            out.append(_trip_to_search_dict(t))
    return out


def resolve_all_trips(page: Page, params: RouteParams) -> list[dict]:
    """
    Enumerate every non-sold-out trip card on the already-loaded search page and
    fetch each one's BusDetails via page.request.get() — the data-urlbusdetails
    attribute is the JSON API endpoint directly, so no browser navigation needed.
    Returns trips de-duplicated by service_id.
    """
    cards = page.locator(".listTripsCard")
    urls: list[str] = []
    for i in range(cards.count()):
        card = cards.nth(i)
        try:
            if "soldOut" in (card.get_attribute("class") or ""):
                continue
            url = card.get_attribute("data-urlbusdetails")
            if url:
                urls.append(url if url.startswith("http") else settings.base_url + url)
        except Exception as exc:
            log.debug(f"[search] card {i} skipped: {exc!r}")
    log.info(f"[search] {len(urls)} trip detail URLs to fetch directly")

    collected: dict[str, dict] = {}
    for idx, url in enumerate(urls):
        try:
            resp = page.request.get(url, timeout=15_000)
            if not resp.ok:
                log.warning(f"[search] detail {idx} HTTP {resp.status}")
                continue
            data = resp.json()
            for trip in _parse_all_trips(data):
                sid = trip["service_id"]
                if sid and sid not in collected:
                    collected[sid] = trip
            jitter(300, 800)
        except Exception as exc:
            log.warning(f"[search] detail {idx} failed: {exc!r}")

    log.info(f"[search] resolved {len(collected)} trips")
    return list(collected.values())


def resolve_trip_direct(page: Page, params: RouteParams) -> dict:
    """
    Read-only fast path for /seats: extracts the matching trip's
    data-urlbusdetails URL from the DOM and fetches BusDetails via
    page.request.get() without navigating the browser.
    Falls back to resolve_trip() if the URL isn't in the DOM or the request fails.
    Not for use in the booking flow — lock_seat_ui() needs the browser on the trip page.
    """
    url = _extract_bus_url(page, params)
    if not url:
        log.warning("[step 2] no matching bus URL in DOM — falling back to resolve_trip")
        return resolve_trip(page, params)

    full_url = url if url.startswith("http") else settings.base_url + url
    log.info(f"[step 2] direct fetch for departure={params.departure}")
    try:
        resp = page.request.get(full_url, timeout=15_000)
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status}")
        data = resp.json()
        result = _parse_bus_details(data, params)
        if result:
            log.info(f"[step 2] direct fetch OK — serviceId={result['serviceId']}")
            debug_seat_map_structure(result["seatMap"])
            return result
        raise RuntimeError("no matching trip in BusDetails response")
    except Exception as exc:
        log.warning(f"[step 2] direct fetch failed: {exc!r} — falling back to resolve_trip")
        return resolve_trip(page, params)
