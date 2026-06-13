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


def _posZ_from(seat: dict, fallback: int) -> float:
    """Read the explicit z/posZ floor field from a seat dict, or use the fallback."""
    for k in ("z", "posZ"):
        v = seat.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return float(fallback)


def parse_seat_map(seat_map: list) -> list[SeatInfo]:
    """Flatten the seatMap into a serialisable list for the /seats endpoint.

    Mobifacil's BusDetails encodes seat position in the 2D array structure:
      outer index n → posX (bus depth, 0=front row)
      inner index i → posY (cross-section, 0=left-window … 4=right-window, 2=corridor)
    posX and posY are ALWAYS the array indices — the 2D structure itself is the
    coordinate system. Trusting any "x"/"y" field on the seat object would break
    layout if BusDetails includes unrelated metadata under those names.
    Empty rows (len==0) are floor separators; we track the floor index and expose
    it as posZ so splitFloors() can group decks correctly.

    Non-numeric labels (WC, ES) and corridor markers (-99) are excluded.
    """
    seats: list[SeatInfo] = []
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
                # int(float(...)) handles both "5" and "5.0" (BusDetails may
                # serialise integers as floats); normalise to a clean int string.
                num_str = str(int(float(num_str)))
            except (ValueError, OverflowError):
                continue  # rejects WC, ES, and any other non-numeric label
            seats.append(SeatInfo(
                number=num_str,
                available=bool(seat.get("disponivel", False)),
                posX=float(n),               # outer index = bus depth (front→back)
                posY=float(i),               # inner index = cross-section (left→right)
                posZ=_posZ_from(seat, floor),
            ))
    return seats


def seat_is_locked(seat_map: list, params: RouteParams) -> bool:
    """Return True iff the seat exists, is not an idoso seat, and is currently locked."""
    target_norm = params.seat.strip().lstrip("0") or "0"
    for seat in _iter_seats(seat_map):
        raw = seat.get("numero", -99)
        if raw == -99 or str(raw) == "-99":
            continue
        num_norm = str(raw).strip().lstrip("0") or "0"
        if num_norm == target_norm or str(raw).strip() == params.seat.strip():
            if seat.get("idoso"):
                return False
            return not bool(seat.get("disponivel", True))
    return False


def check_seat_availability(seat_map: list, params: RouteParams) -> bool:
    target = params.seat
    target_norm = target.strip().lstrip("0") or "0"
    for seat in _iter_seats(seat_map):
        raw = seat.get("numero", -99)
        if raw == -99 or str(raw) == "-99":
            continue
        num_norm = str(raw).strip().lstrip("0") or "0"
        if num_norm == target_norm or str(raw).strip() == target:
            # Priority (idoso) seats are reservable only via attendance — mobifacil's
            # own UI blocks them client-side, and a LockSeat POST would be refused.
            # Treat as unavailable so we fail fast with a clear reason.
            if seat.get("idoso"):
                log.warning(
                    f"[step 3] seat '{target}' is a priority/idoso seat — "
                    "reservable only via attendance, not bookable here"
                )
                return False
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

        # Pair fields explicitly so posX is never matched with an unrelated "y"
        # field (e.g. a CSS layout value), which can produce wildly off-screen coords.
        for x_field, y_field in [("posX", "posY"), ("x", "y"), ("cx", "cy"), ("left", "top"), ("col", "row")]:
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
    svg_count = svg_texts.count()
    log.debug(f"[step 4] SVG text elements on page: {svg_count}")
    matched_svg_texts: list[str] = []
    for i in range(svg_count):
        try:
            text_el = svg_texts.nth(i)
            text = (text_el.text_content() or "").strip()
            if text:
                matched_svg_texts.append(repr(text))
            if text == target or text.lstrip("0") == target.lstrip("0"):
                log.info(f"[seatmap] found SVG text '{text}' matching seat {target}")
                parent = text_el.locator("..")
                try:
                    parent.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                parent.click(timeout=5000, force=True)
                jitter(500, 1000)
                return True
        except Exception:  # FIX (bug 2)
            continue
    if matched_svg_texts:
        log.debug(f"[step 4] SVG texts sample (target='{target}'): {', '.join(matched_svg_texts[:8])}")

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
            if page.locator(selector).count() > 0:
                el = page.locator(selector).first
                if el.is_visible(timeout=2000):
                    log.info(f"[seatmap] found seat via selector: {selector}")
                    try:
                        el.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
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
                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                el.click(timeout=5000, force=True)
                jitter(500, 1000)
                return True
    except Exception:  # FIX (bug 2)
        pass

    log.warning(f"[step 4] DOM/SVG search exhausted — seat '{target}' not found in UI")
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
            container = containers.nth(i)
            box = container.bounding_box()
            if not (box and box["width"] > 100 and box["height"] > 100):
                continue
            click_x = box["x"] + coords[0]
            click_y = box["y"] + coords[1]
            # Guard: coords from BusDetails are grid indices, not pixel offsets — the
            # computed position can land completely outside the container. Skip and let
            # lock_seat_api handle it instead of firing a blind click.
            if not (box["x"] <= click_x <= box["x"] + box["width"] and
                    box["y"] <= click_y <= box["y"] + box["height"]):
                log.warning(
                    f"[step 4] coord ({click_x:.0f}, {click_y:.0f}) outside container "
                    f"bounds ({box['x']:.0f},{box['y']:.0f} "
                    f"+{box['width']:.0f}x{box['height']:.0f}) — skipping"
                )
                continue
            try:
                container.scroll_into_view_if_needed(timeout=2000)
                box = container.bounding_box() or box  # refresh after scroll
                click_x = box["x"] + coords[0]
                click_y = box["y"] + coords[1]
            except Exception:
                pass
            log.info(f"[step 4] clicking at ({click_x:.0f}, {click_y:.0f})")
            page.mouse.click(click_x, click_y)
            jitter(500, 1000)
            _click_proceed_button(page, ["button:has-text('Continuar')",
                                         "button:has-text('Finalizar compra')"])
            return True
        except Exception:  # FIX (bug 2)
            continue
    log.warning("[step 4] coordinate click: no valid in-bounds container — falling back to API")
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


def _info_connection(trip: dict) -> str:
    """
    Build LockSeat's ``infoConnection`` value, mirroring mobifacil's frontend
    (``infoConnection = busDetails.objConnection || null``). The server
    ``JSON.parse``s this field, so it must always be present and parseable:
    the real connection object as JSON when the trip has one, otherwise the
    literal string ``"null"`` (which ``JSON.parse`` reads as null). Never ""
    and never absent — either makes the server parse ``undefined`` and fail.
    """
    obj_conn = trip.get("objConnection")
    if obj_conn:
        return json.dumps(obj_conn, separators=(",", ":"))
    return "null"


def _resolve_seat_label(trip: dict, requested: str) -> str:
    """
    Mobifacil's LockSeat sends ``seat = t.numero`` — the seatMap's RAW label,
    which is zero-padded ("05"), not the normalised "5" our /seats endpoint
    exposes (parse_seat_map strips the leading zero). Map the requested seat back
    to the exact ``numero`` string from the seatMap so the server matches it;
    fall back to the requested value if the seat isn't found.
    """
    target = requested.strip().lstrip("0") or "0"
    seat_map = trip.get("seatMap") or trip.get("seatsWithPrice") or []
    for seat in _iter_seats(seat_map):
        raw = seat.get("numero")
        if raw in (-99, "-99", None):
            continue
        num = str(raw).strip()
        if num == requested.strip() or (num.lstrip("0") or "0") == target:
            return num
    return requested.strip()


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
        # seat must be the seatMap's raw numero ("05"), matching mobifacil's
        # `seat = t.numero`, not the normalised request value ("5").
        "seat": _resolve_seat_label(trip, params.seat),
        "arrival": trip.get("arrival", trip.get("arrivalHour", "")),
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
        # The server JSON.parses infoConnection (LockSeatModel.js:115). Mobifacil's
        # own frontend ALWAYS appends it: `infoConnection = objConnection || null`,
        # which URLSearchParams coerces to the literal string "null" for
        # non-connection trips. Sending "" — or dropping the field via the
        # empty-filter below — makes the server parse `undefined`, producing
        # "Unexpected token: u" (JSON.parse(undefined)). Always send a parseable
        # value: real connection JSON when present, else the string "null".
        "infoConnection": _info_connection(trip),
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
        # Headers mirror mobifacil's own fetch: only Content-Type. (Referer is kept
        # for anti-bot parity — a real browser would send it automatically.)
        resp = page.request.post(
            settings.base_url + LOCK_SEAT_PATH,
            form=payload,
            headers={
                "Referer": settings.base_url + "/passagem-de-onibus/",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=25_000,
        )
        # Transient/infra problems are worth retrying (the seat may free, the edge
        # may hiccup). A 200 with a server-level error is a *decision*, not a glitch.
        if resp.status != 200:
            raise RuntimeError(f"LockSeat API HTTP {resp.status}")
        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to parse LockSeat response: {e}") from e

        log.info(f"[step 4] LockSeat response: {json.dumps(data)[:400]}")
        if data.get("error") or not data.get("success"):
            error_msg = data.get("message", "Unknown error")
            title = data.get("title", "")
            if "Unexpected token" in error_msg:
                # Server couldn't parse our payload — retrying won't help, and a
                # malformed payload is a code bug, not a busy seat. Log loudly and
                # stop (return False → exit 1) instead of resetting the browser.
                log.error("[step 4] Server JSON parse error — malformed payload (bug)")
                log.error(f"[step 4] Full payload sent: {json.dumps(payload)}")
                return False
            # Business decline (seat taken / not lockable). Mirrors the frontend,
            # which just surfaces o.error. Don't retry or reset the browser — report
            # unavailable so the scheduler retries on its normal interval.
            log.warning(f"[step 4] LockSeat declined: {title or error_msg}")
            return False

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
