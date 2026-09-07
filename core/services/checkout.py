"""
Steps 5-6 of the flow: best-effort checkout visit (to commit the hold the way
the browser flow does) and a non-vetoing lock corroboration via page-content /
BusDetails re-fetch.

The authoritative confirmation is the LockSeat ``seatUUID`` returned by
``seat.lock_seat`` — these helpers only *corroborate* it. mobifacil's seat map
is a cached endpoint that lags a fresh hold, so a "still available" reading here
must NEVER override a valid seatUUID (that flip-flop was the old false-negative
that polled for 60s and then failed real locks).
"""
from __future__ import annotations

from typing import Optional

from playwright.sync_api import Page

from ..config import BUS_DETAILS_PATH, CHECKOUT_PATH, settings
from ..models.schemas import RouteParams
from ..utils.logger import log
from .browser import check_detection, jitter, retry
from .seatmap import find_seat


# ─────────────────────────────────────────────────────────────────
# Step 5 — Checkout (best-effort hold commit)
# ─────────────────────────────────────────────────────────────────
def proceed_to_checkout(page: Page) -> None:
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
    # There used to be a `telemetry.wait_for(timeout=5.0)` here. Production logs
    # showed the fingerprint response it waits on never arrives (0 sightings in 11
    # runs), so it consumed its full timeout every time — 5 s per flow spent waiting
    # for an event that no longer exists, while the seat was already held by the
    # LockSeat POST. See browser.TelemetryWatcher for the diagnostics that replaced it.


# ─────────────────────────────────────────────────────────────────
# Step 7 — Confirm lock
# ─────────────────────────────────────────────────────────────────
def _confirm_via_content(page: Page, params: RouteParams) -> bool:
    try:
        page_content = page.content()[:5000].lower()
    except Exception as exc:  # FIX (bug 2)
        log.debug(f"[step 7] content read failed: {exc!r}")
        return False
    # "checkout", "finalizar", "pagamento" are present on every checkout page visit
    # regardless of lock success — they are not confirmation signals.
    indicators = [
        "reserva confirmada", "poltrona reservada", "assento reservado",
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
    # Padded/unpadded matching lives in seatmap.seat_matches — an exact-string
    # compare here silently missed the seat and fell through to "assume locked".
    cell = find_seat(seat_map, params.seat)
    if cell is None:
        return None
    avail = cell.get("disponivel", True)
    log.info(f"[step 7] recheck seat '{params.seat}': disponivel={avail}")
    return not avail


def corroborate_lock(page: Page, trip: dict, params: RouteParams) -> Optional[bool]:
    """Best-effort secondary check that the seat reads as locked.

    Returns ``True`` (page/API shows it held), ``False`` (still shows available —
    almost always cache lag, NOT a real failure), or ``None`` (couldn't tell).
    This is corroboration only: the caller already holds an authoritative
    seatUUID and must never let a ``False``/``None`` here veto a real lock.
    """
    log.info(f"[step 6] corroborating seat {params.seat} lock (non-vetoing)")

    # STRATEGY 1: page content — specific reservation confirmation phrases only.
    if _confirm_via_content(page, params):
        return True

    # STRATEGY 2: API recheck — seat shows disponivel=false iff the hold is visible.
    try:
        return _confirm_via_api(page, trip, params)
    except Exception as exc:
        log.debug(f"[step 6] API recheck failed: {exc!r}")
        return None
