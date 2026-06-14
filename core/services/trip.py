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
from .htmlsearch import _parse_lsservicos, build_bus_details_url, filter_trips_by_date


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
        # "load" instead of "networkidle": analytics/tracking scripts keep the
        # network busy indefinitely and cause networkidle to always time out.
        # .wait_for_selector below handles waiting for the actual trip content.
        page.goto(params.search_url, wait_until="load", timeout=55_000)
        if "mobifacil" not in page.url:
            raise RuntimeError(f"Unexpected redirect: {page.url}")
        check_detection(page, "search_load")
        names = [c["name"] for c in page.context.cookies()]
        dw = [k for k in names if k.startswith("dw") or k == "sid"]
        if not dw:
            raise RuntimeError("Session cookies absent")
        log.info(f"[step 1] session cookies: {dw}")
        try:
            page.wait_for_selector(".listTripsCard", timeout=30_000)
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

    arr_hour = trip.get("arrivalHour", "")
    return {
        "serviceId": sid,
        "fareId": fare_id,
        "fareCode": fare_code,
        "empresaId": empresa_id,
        "departureHour": dep,
        "arrivalHour": arr_hour,
        # LockSeat's createDateObjects parses `arrival` and `departure` as FULL
        # datetimes ("dd/mm/yyyy HH:MM:SS"). BusDetails already provides arrival in
        # that form (same source/format as `departure` below) — use it. The old code
        # overrode it with the bare "HH:MM" arrivalHour, which the server rejects:
        # "createDateObjects ... The specified timeString could not be parsed". Fall
        # back to arrivalHour only if BusDetails omits the full value.
        "arrival": str(trip.get("arrival") or arr_hour),
        "service": f"{sid}-{params.date}T{params.departure}-{fare_code}",
        "seatMap": trip.get("seatMap", []),
        "hasSecondFloor": bool(trip.get("hasSecondFloor", False)),
        "preco": str(trip.get("price") or ""),
        "company": trip.get("company", ""),
        # BusDetails carries the RESOLVED terminal IDs under "origin"/"destination"
        # (e.g. 21787), NOT the search meta-origin the user typed (e.g. -3, "all
        # São Paulo"). Mobifacil's LockSeat locks against these terminal IDs
        # (resolveField(r,"origin","origemId")), so prefer them; fall back to the
        # *IdDistribusion ids, then the request's values as a last resort.
        "originId": str(
            trip.get("origin") or trip.get("originIdDistribusion") or params.origin_id
        ),
        "destinationId": str(
            trip.get("destination")
            or trip.get("destinationIdDistribusion")
            or params.destination_id
        ),
        "group": trip.get("group", "TOTAL_BUS"),
        "raceDate": trip.get("raceDate", params.date),
        "rutaId": str(trip.get("rutaId", "")),
        "serviceClass": trip.get("serviceClass", ""),
        "originUf": trip.get("originUf", ""),
        "stepNumber": str(trip.get("stepNumber", "1")),
        "offerId": str(trip.get("offerId", "")),
        "connectionId": str(trip.get("connectionId", "")),
        # isDistribusion is a Python bool from BusDetails; str(True) = "True"
        # which the server rejects — always lower-case.
        "isDistribusion": str(trip.get("isDistribusion", True)).lower(),
        # Mobifacil sets busMap.seatsWithPrice = trip.seatMap (the raw 2D array).
        # BusDetails never returns a "seatsWithPrice" field — use seatMap directly.
        "seatsWithPrice": trip.get("seatMap", []),
        "departure": trip.get("departure", dep),
        # objConnection lives on details (sibling of trip), not on the trip. The
        # frontend POSTs it as LockSeat's infoConnection field; it is null for
        # non-connection trips. Captured here so the lock payload can send a
        # JSON-parseable value (see seat._build_lock_payload).
        "objConnection": data.get("details", {}).get("objConnection"),
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


def _dep_hour(ls_trip: dict) -> str:
    """Departure HH:MM from an lsServicos entry's ``saida`` ("DD/MM/YYYY HH:MM")."""
    saida = ls_trip.get("saida", "")
    return saida.rsplit(" ", 1)[-1] if " " in saida else saida


def _resolve_trip_direct(page: Page, params: RouteParams) -> Optional[dict]:
    """Resolve the trip WITHOUT a UI click or XHR-intercept race.

    Parses lsServicos from the already-loaded search-page HTML, builds the
    BusDetails URL exactly as a card click would, and fetches it through the
    browser's OWN request context — so cookies are shared with the eventual
    LockSeat POST. This is the same deterministic path ``/seats`` uses; it
    replaces the flaky "intercept not captured" failure mode. Returns the trip
    dict, or ``None`` to let the caller fall back to the intercept method.
    """
    try:
        html = page.content()
    except Exception as exc:
        log.warning(f"[step 2] direct: could not read page HTML: {exc!r}")
        return None

    trips = _parse_lsservicos(html)
    if not trips:
        log.warning("[step 2] direct: no lsServicos in page HTML")
        return None

    trips = filter_trips_by_date(trips, params.date)
    if not trips:
        log.warning("[step 2] direct: no trips for requested date in HTML")
        return None

    matching = next((t for t in trips if _dep_hour(t) == params.departure), None)
    if not matching:
        available = [_dep_hour(t) for t in trips]
        log.warning(f"[step 2] direct: departure {params.departure} not in HTML trips {available}")
        return None

    url = build_bus_details_url(matching, params.date)
    log.info("[step 2] direct: fetching BusDetails (no UI click)")
    try:
        resp = page.request.get(
            url,
            headers={
                "Referer": settings.base_url + "/passagem-de-onibus/",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=20_000,
        )
        if resp.status != 200:
            log.warning(f"[step 2] direct: BusDetails HTTP {resp.status}")
            return None
        data = resp.json()
    except Exception as exc:
        log.warning(f"[step 2] direct: BusDetails fetch failed: {exc!r}")
        return None

    result = _parse_bus_details(data, params)
    if not result:
        log.warning("[step 2] direct: BusDetails parse returned no usable trip")
        return None

    log.info(f"[step 2] trip resolved via direct BusDetails — serviceId={result['serviceId']}")
    debug_seat_map_structure(result["seatMap"])
    return result


def resolve_trip(page: Page, params: RouteParams) -> dict:
    """Resolve the target trip dict. Direct HTTP path first (deterministic),
    XHR-intercept click path as a fallback."""
    log.info("[step 2] resolving trip")

    direct = _resolve_trip_direct(page, params)
    if direct:
        return direct

    log.warning("[step 2] direct resolution failed — falling back to XHR intercept")
    return _resolve_trip_via_intercept(page, params)


def _resolve_trip_via_intercept(page: Page, params: RouteParams) -> dict:
    log.info("[step 2] resolving trip via XHR intercept (fallback)")

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
