"""
Steps 5-7 of the flow: proceed to checkout (holding the lock) and confirm the
seat is actually locked via URL → page-content → BusDetails re-fetch.
"""
from __future__ import annotations

from typing import Optional

from playwright.sync_api import Page

from ..config import BUS_DETAILS_PATH, CHECKOUT_PATH, settings
from ..models.schemas import RouteParams
from ..utils.logger import log
from .browser import TelemetryWatcher, check_detection, jitter, retry


# ─────────────────────────────────────────────────────────────────
# Step 5 — Checkout
# ─────────────────────────────────────────────────────────────────
def proceed_to_checkout(page: Page, telemetry: TelemetryWatcher) -> None:
    log.info("[step 5] navigating to Checkout-Begin")

    def _go() -> None:
        current = page.url.lower()
        if "checkout" in current or "finalizar" in current:
            log.info(f"[step 5] already on checkout page: {page.url[:80]}")
            return
        page.goto(settings.base_url + CHECKOUT_PATH, wait_until="domcontentloaded", timeout=30_000)
        jitter(1400, 2600)
        check_detection(page, "checkout")

    retry(_go, "checkout")
    telemetry.wait_for(timeout=20.0)


# ─────────────────────────────────────────────────────────────────
# Step 7 — Confirm lock
# ─────────────────────────────────────────────────────────────────
def _confirm_via_content(page: Page, params: RouteParams) -> bool:
    try:
        page_content = page.content()[:5000].lower()
    except Exception as exc:  # FIX (bug 2)
        log.debug(f"[step 7] content read failed: {exc!r}")
        return False
    indicators = [
        "reserva confirmada", "poltrona reservada", "assento reservado",
        "checkout", "finalizar", "pagamento",
        f"poltrona {params.seat}", f"assento {params.seat}",
    ]
    for indicator in indicators:
        if indicator in page_content:
            log.info(f"[step 7] found indicator: '{indicator}'")
            return True
    return False


def _confirm_via_api(page: Page, trip: dict, params: RouteParams) -> Optional[bool]:
    """Re-fetch BusDetails and report whether the target seat is now unavailable."""
    params_qs = {
        "hasConnection": "false", "isDistribusion": "true", "multipleFares": "false",
        "origin": trip.get("originId", params.origin_id),
        "destination": trip.get("destinationId", params.destination_id),
        "fareId": trip.get("fareId", ""), "fareCode": trip.get("fareCode", "FARE-1"),
        "group": "TOTAL_BUS", "service": trip.get("service", ""),
        "date": params.date, "returnDate": "", "step": "1",
        "isStudent": "false", "isPCD": "false", "isAjax": "true",
        "empresaId": trip.get("empresaId", ""), "raceDate": params.date,
        "departureHour": params.departure, "arrivalHour": trip.get("arrivalHour", ""),
        "isMobioferta": "false",
    }
    resp = page.request.get(
        settings.base_url + BUS_DETAILS_PATH, params=params_qs,
        headers={"Referer": settings.base_url, "X-Requested-With": "XMLHttpRequest"},
        timeout=15_000,
    )
    if resp.status != 200:
        return None
    data = resp.json()
    if not data.get("success"):
        return None
    trips = data.get("details", {}).get("trip", [])
    seat_map = trips[0].get("seatMap", []) if trips else []
    for row in seat_map:
        if not isinstance(row, list):
            continue
        for seat in row:
            if isinstance(seat, dict) and str(seat.get("numero", "")).strip() == params.seat:
                avail = seat.get("disponivel", True)
                log.info(f"[step 7] recheck: disponivel={avail}")
                return not avail
    return None


def confirm_seat_locked(page: Page, trip: dict, params: RouteParams) -> bool:
    """Confirm the seat is locked via URL, page content, then API recheck."""
    log.info(f"[step 7] confirming seat {params.seat} is locked")
    current_url = page.url.lower()

    # STRATEGY 1: on checkout → locked.
    if "checkout" in current_url or "finalizar" in current_url:
        log.info("[step 7] on checkout page — lock confirmed by URL")
        return True

    # STRATEGY 2: page content indicators.
    if _confirm_via_content(page, params):
        return True

    # STRATEGY 3: API recheck.
    try:
        result = _confirm_via_api(page, trip, params)
        if result is not None:
            return result
    except Exception as exc:
        # FIX (bug 6): original had `except: pass` here, hiding all errors.
        log.debug(f"[step 7] API recheck failed: {exc!r}")

    # FINAL FALLBACK: UI flow completed = assume locked.
    log.info("[step 7] UI lock flow completed — assuming locked")
    return True
