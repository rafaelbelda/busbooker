"""
Steps 3-4 of the flow: seat-availability check and seat locking (UI click with
coordinate fallback, then a direct LockSeat API POST as a last resort).
"""
from __future__ import annotations

import json
import random
from typing import Optional, Tuple

from playwright.sync_api import Page

from ..config import LOCK_SEAT_PATH, settings
from ..models.schemas import RouteParams, SeatInfo
from ..utils.logger import log
from .browser import jitter, retry


# ─────────────────────────────────────────────────────────────────
# Step 3 — Availability
# ─────────────────────────────────────────────────────────────────
def _iter_seats(seat_map: list):
    """Yield every dict seat in the map, skipping non-list rows / non-dict cells."""
    for row in seat_map:
        if not isinstance(row, list):
            continue
        for seat in row:
            # FIX (bug B): original check_seat_availability called seat.get()
            # without guarding that the cell is a dict, unlike the coordinate
            # extractor — a non-dict cell raised AttributeError.
            if isinstance(seat, dict):
                yield seat


def parse_seat_map(seat_map: list) -> list[SeatInfo]:
    """Flatten the seatMap into a serialisable list for the /seats endpoint."""
    seats: list[SeatInfo] = []
    for seat in _iter_seats(seat_map):
        raw = seat.get("numero", -99)
        if raw == -99 or str(raw) == "-99":
            continue
        seats.append(SeatInfo(number=str(raw).strip(), available=bool(seat.get("disponivel", False))))
    return seats


def check_seat_availability(seat_map: list, params: RouteParams) -> bool:
    target = params.seat
    target_norm = target.strip().lstrip("0") or "0"
    for seat in _iter_seats(seat_map):
        raw = seat.get("numero", -99)
        if raw == -99 or str(raw) == "-99":
            continue
        num_norm = str(raw).strip().lstrip("0") or "0"
        if num_norm == target_norm or str(raw).strip() == target:
            avail = seat.get("disponivel", False)
            log.info(f"[step 3] seat '{target}' → disponivel={avail}")
            return bool(avail)
    log.warning(f"[step 3] seat '{target}' not found in seatMap")
    return False


# ─────────────────────────────────────────────────────────────────
# Coordinate extraction
# ─────────────────────────────────────────────────────────────────
def extract_seat_coordinates(seat_map: list, seat_number: str) -> Optional[Tuple[float, float]]:
    """Extract seat coordinates from seatMap data, trying multiple field names."""
    target = str(seat_number).strip()

    for seat in _iter_seats(seat_map):
        seat_num = str(seat.get("numero", "")).strip()
        if seat_num != target:
            continue

        log.info(f"[seatmap] found seat {seat_number} in data: {json.dumps(seat)[:300]}")

        for x_field in ["posX", "x", "cx", "left", "col"]:
            for y_field in ["posY", "y", "cy", "top", "row"]:
                x = seat.get(x_field)
                y = seat.get(y_field)
                if x is not None and y is not None:
                    log.info(f"[seatmap] using coordinates: {x_field}={x}, {y_field}={y}")
                    return (float(x), float(y))

        coord = seat.get("coordinate") or seat.get("coord") or seat.get("position")
        if coord and isinstance(coord, str) and "," in coord:
            parts = coord.split(",")
            if len(parts) == 2:
                try:
                    return (float(parts[0]), float(parts[1]))
                except (ValueError, TypeError):  # FIX (bug 2): scoped, not bare
                    pass

        col = seat.get("coluna") or seat.get("column") or seat.get("col")
        row_idx = seat.get("fileira") or seat.get("row")
        if col is not None and row_idx is not None:
            x = float(col) * 40 + 20  # Assume 40px per seat
            y = float(row_idx) * 40 + 20
            log.info(f"[seatmap] estimated grid pos: col={col}, row={row_idx} → ({x}, {y})")
            return (x, y)

        log.warning(f"[seatmap] seat {seat_number} found but no coordinate fields")
        log.info(f"[seatmap] available fields: {list(seat.keys())}")
        return None

    log.warning(f"[seatmap] seat {seat_number} not found in seatMap")
    return None


# ─────────────────────────────────────────────────────────────────
# UI clicking helpers
# ─────────────────────────────────────────────────────────────────
_PROCEED_SELECTORS = [
    "button:has-text('Finalizar compra')",
    "button:has-text('Continuar')",
    "button:has-text('Prosseguir')",
    "a:has-text('Finalizar compra')",
    "a:has-text('Continuar')",
    "[data-action='continue']",
]


def _click_proceed_button(page: Page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=5000):
                log.info(f"[step 4] found proceed button: {sel}")
                btn.click(timeout=5000, force=True)
                jitter(1000, 2000)
                return True
        except Exception:  # FIX (bug 2)
            continue
    return False


def find_clickable_seat_in_ui(page: Page, seat_number: str) -> bool:
    """Find and click the seat in the UI using DOM/SVG/aria selectors."""
    target = str(seat_number).strip()

    svg_texts = page.locator("svg text, svg tspan")
    for i in range(svg_texts.count()):
        try:
            text_el = svg_texts.nth(i)
            text = (text_el.text_content() or "").strip()
            if text == target or text.lstrip("0") == target.lstrip("0"):
                log.info(f"[seatmap] found SVG text '{text}' matching seat {target}")
                text_el.locator("..").click(timeout=5000, force=True)
                jitter(500, 1000)
                return True
        except Exception:  # FIX (bug 2)
            continue

    attr_selectors = [
        f"[data-seat='{target}']",
        f"[data-seat-number='{target}']",
        f"[data-seat-id*='{target}']",
        f"[id*='seat-{target}']",
        f"[id*='seat_{target}']",
        f"[id*='poltrona-{target}']",
    ]
    for selector in attr_selectors:
        try:
            el = page.locator(selector).first
            if el.count() and el.is_visible(timeout=2000):
                log.info(f"[seatmap] found seat via selector: {selector}")
                el.click(timeout=5000, force=True)
                jitter(500, 1000)
                return True
        except Exception:  # FIX (bug 2)
            continue

    try:
        all_elements = page.locator("[aria-label]")
        for i in range(all_elements.count()):
            el = all_elements.nth(i)
            label = el.get_attribute("aria-label") or ""
            if target in label or target.lstrip("0") in label:
                log.info(f"[seatmap] found seat via aria-label: {label}")
                el.click(timeout=5000, force=True)
                jitter(500, 1000)
                return True
    except Exception:  # FIX (bug 2)
        pass

    return False


# ─────────────────────────────────────────────────────────────────
# Step 4 — Lock seat
# ─────────────────────────────────────────────────────────────────
def _lock_via_coordinates(page: Page, trip: dict, params: RouteParams) -> bool:
    coords = extract_seat_coordinates(trip["seatMap"], params.seat)
    if not coords:
        return False
    containers = page.locator("canvas, svg, [class*='busMap'], [class*='seatmap']")
    for i in range(containers.count()):
        try:
            box = containers.nth(i).bounding_box()
            if box and box["width"] > 100 and box["height"] > 100:
                click_x = box["x"] + coords[0]
                click_y = box["y"] + coords[1]
                log.info(f"[step 4] clicking at ({click_x:.0f}, {click_y:.0f})")
                page.mouse.click(click_x, click_y)
                jitter(500, 1000)
                _click_proceed_button(page, ["button:has-text('Continuar')",
                                             "button:has-text('Finalizar compra')"])
                return True
        except Exception:  # FIX (bug 2)
            continue
    return False


def lock_seat_ui(page: Page, trip: dict, params: RouteParams) -> bool:
    """Try to lock the seat through UI interaction."""
    log.info(f"[step 4] attempting UI seat lock for seat {params.seat}")

    if find_clickable_seat_in_ui(page, params.seat):
        log.info("[step 4] seat clicked via DOM selector")
        jitter(1000, 2000)
        if _click_proceed_button(page, _PROCEED_SELECTORS):
            return True

    return _lock_via_coordinates(page, trip, params)


def _coerce_seats_with_price(trip: dict) -> str:
    """
    FIX (bug 4): the original sent ``str(trip.get("seatsWithPrice", ""))``.
    For a list/dict that yields a Python repr with single quotes — invalid JSON
    that triggers the server's "Unexpected token" error — and ``str([])`` leaks
    a useless ``"[]"`` past the later empty-string filter. Coerce to real JSON
    (or an empty string) up front so the filter behaves correctly.
    """
    raw = trip.get("seatsWithPrice", "")
    if isinstance(raw, (list, dict)):
        return json.dumps(raw, separators=(",", ":")) if raw else ""
    return str(raw) if raw not in (None, "") else ""


def _build_lock_payload(trip: dict, params: RouteParams) -> dict:
    seats_with_price = _coerce_seats_with_price(trip)
    payload = {
        "busNumber": "firstBus",
        "origin": trip.get("originId", params.origin_id),
        "destination": trip.get("destinationId", params.destination_id),
        "date": params.date,
        "service": trip["service"],
        "departureHour": params.departure,
        "group": trip.get("group", "TOTAL_BUS"),
        "seat": params.seat,
        "arrival": trip.get("arrival", trip.get("arrivalHour", "11:30")),
        "company": trip.get("company", ""),
        "departure": trip.get("departure", params.departure),
        "originUf": trip.get("originUf", ""),
        "serviceClass": trip.get("serviceClass", ""),
        "step": trip.get("stepNumber", "1"),
        "rutaId": trip.get("rutaId", ""),
        "empresaId": trip["empresaId"],
        "isUpsell": "false",
        "upsellOriginalClass": trip.get("serviceClass", ""),
        "upsellOriginalPrice": trip.get("preco", ""),
        "upsellOriginalServiceNo": trip["service"],
        "upsellOriginalTime": params.departure.replace(":", ""),
        "seatMap": seats_with_price,
        "infoConnection": "",
        "raceDate": trip.get("raceDate", params.date),
        "fareId": trip["fareId"],
        "fareCode": trip.get("fareCode", "FARE-1"),
        "offerId": trip.get("offerId", ""),
        "connectionId": trip.get("connectionId", ""),
        "isDistribusion": trip.get("isDistribusion", "true"),
        "isWebView": "false",
        "isMobile": "false",
    }
    # Drop empties (now safe: seatMap is real JSON or a true empty string).
    return {k: v for k, v in payload.items() if v is not None and v != ""}


def lock_seat_api(page: Page, trip: dict, params: RouteParams) -> bool:
    """API fallback for seat locking — posts the frontend's URLSearchParams form."""
    log.info("[step 4] API fallback — POSTing LockSeat")
    payload = _build_lock_payload(trip, params)
    log.info(f"[step 4] LockSeat payload keys: {list(payload.keys())}")
    log.info(f"[step 4] LockSeat payload: {json.dumps(payload, indent=2)[:500]}")

    def _post() -> bool:
        resp = page.request.post(
            settings.base_url + LOCK_SEAT_PATH,
            form=payload,
            headers={
                "Referer": settings.base_url + "/passagem-de-onibus/",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=25_000,
        )
        if resp.status != 200:
            raise RuntimeError(f"LockSeat API HTTP {resp.status}")
        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to parse LockSeat response: {e}") from e

        log.info(f"[step 4] LockSeat response: {json.dumps(data)[:400]}")
        if data.get("error") or not data.get("success"):
            error_msg = data.get("message", "Unknown error")
            if "Unexpected token" in error_msg:
                log.error("[step 4] Server JSON parse error — likely malformed payload")
                log.error(f"[step 4] Full payload sent: {json.dumps(payload)}")
            raise RuntimeError(f"LockSeat failed: {error_msg}")

        log.info(f"[step 4] LockSeat success — uuid={data.get('seatUUID')}")
        return True

    return retry(_post, "api_seat_lock", attempts=3)


def lock_seat(page: Page, trip: dict, params: RouteParams) -> bool:
    """Main seat locking function: UI first, API fallback."""
    log.info("[step 4] stimulating interactions before lock")
    for _ in range(3):
        try:
            page.mouse.move(random.randint(300, 800), random.randint(300, 600))
            jitter(200, 500)
        except Exception:  # FIX (bug 2)
            pass

    if lock_seat_ui(page, trip, params):
        log.info(f"[step 4] seat {params.seat} locked via UI")
        return True

    log.warning("[step 4] UI lock failed — falling back to API")
    return lock_seat_api(page, trip, params)
