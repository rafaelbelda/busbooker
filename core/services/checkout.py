"""
Steps 5-7 of the flow: proceed to checkout (holding the lock) and confirm the
seat is actually locked via URL → page-content → BusDetails re-fetch.
"""
from __future__ import annotations

import random
import time
from typing import Optional

from playwright.sync_api import Page

from ..config import BUS_DETAILS_PATH, CHECKOUT_PATH, settings
from ..models.schemas import RouteParams
from ..utils.logger import log
from .browser import TelemetryWatcher, check_detection, jitter, retry, stochastic_idle


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
    target_norm = params.seat.strip().lstrip("0") or "0"
    for row in seat_map:
        if not isinstance(row, list):
            continue
        for seat in row:
            if not isinstance(seat, dict):
                continue
            raw = str(seat.get("numero", "")).strip()
            # seatMap labels are zero-padded ("05"); the request seat is normalised
            # ("5"). Match on both forms so the recheck finds the seat (exact-match
            # would silently miss it and fall through to "assume locked").
            if raw == params.seat.strip() or (raw.lstrip("0") or "0") == target_norm:
                avail = seat.get("disponivel", True)
                log.info(f"[step 7] recheck seat '{params.seat}': disponivel={avail}")
                return not avail
    return None


def wait_for_lock_confirmation(page: Page, trip: dict, params: RouteParams) -> bool:
    """Poll the API every ~10s until the seat shows locked, or cap is reached."""
    cap = settings.wait_after_lock
    interval = 10.0
    start = time.monotonic()
    attempt = 0

    log.info(f"[step 6] polling for lock confirmation (cap={cap}s, every ~{interval:.0f}s)")

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= cap:
            break

        attempt += 1
        stochastic_idle(page, f"step-6-idle-{attempt}")

        try:
            result = _confirm_via_api(page, trip, params)
        except Exception as exc:
            log.debug(f"[step 6] poll {attempt}: API error {exc!r}")
            result = None

        elapsed = time.monotonic() - start
        if result is True:
            log.info(f"[step 6] lock confirmed on poll {attempt} ({elapsed:.1f}s elapsed)")
            return True

        log.debug(f"[step 6] poll {attempt}: not confirmed yet ({elapsed:.1f}s elapsed)")

        remaining = cap - elapsed
        if remaining <= 0:
            break
        time.sleep(min(interval + random.uniform(-1.5, 1.5), remaining))

    elapsed = time.monotonic() - start
    log.info(f"[step 6] lock poll cap reached ({elapsed:.1f}s) — proceeding to step 7")
    return False


def confirm_seat_locked(page: Page, trip: dict, params: RouteParams) -> bool:
    """Confirm the seat is locked via page content or API recheck."""
    log.info(f"[step 7] confirming seat {params.seat} is locked")

    # STRATEGY 1: page content — specific reservation confirmation phrases only.
    # (URL "checkout"/"finalizar" check removed: step 5 always navigates there,
    # so the URL is always present and cannot distinguish a successful lock.)
    if _confirm_via_content(page, params):
        return True

    # STRATEGY 2: API recheck — authoritative; seat shows disponivel=false iff locked.
    try:
        result = _confirm_via_api(page, trip, params)
        if result is not None:
            return result
    except Exception as exc:
        log.debug(f"[step 7] API recheck failed: {exc!r}")

    log.warning(f"[step 7] seat {params.seat} — no confirmation strategy succeeded")
    return False
