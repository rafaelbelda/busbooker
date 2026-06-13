"""
Flow orchestration.

``run_flow`` is the single self-contained, **synchronous** entrypoint used by
both the API (via a thread-pool executor) and the scheduler. It owns the
Playwright lifecycle, profile validation and the error-retry that the original
``main()`` performed, and returns ``(exit_code, trip_dict | None)`` where the
exit code is:

    0  →  seat locked and confirmed   (trip_dict is the resolved trip)
    1  →  seat unavailable / lock failed   (trip_dict is None)
    2  →  unrecoverable flow error   (trip_dict is None)

The trip dict (departureHour, date, arrivalHour, …) lets the caller compute the
reservation's departure_datetime for the re-lock scheduler.
"""
from __future__ import annotations

import random
import time

from playwright.sync_api import Page, sync_playwright

from ..config import settings
from ..models.schemas import RouteParams, SeatInfo
from ..utils.logger import log, reservation_log
from .browser import (
    TelemetryWatcher,
    _profile_looks_valid,
    build_context,
    jitter,
    reset_profile,
    stochastic_idle,
)
from .checkout import confirm_seat_locked, proceed_to_checkout, wait_for_lock_confirmation
from .seat import check_seat_availability, lock_seat, parse_seat_map
from .trip import open_search_page, resolve_trip


# ─────────────────────────────────────────────────────────────────
# Param resolution
# ─────────────────────────────────────────────────────────────────
def resolve_route_params(
    origin_id: str,
    destination_id: str,
    date: str,
    departure: str,
    seat: str = "",
) -> RouteParams:
    """Build frozen RouteParams from explicit, user-supplied route values.

    There is no config fallback: every route value originates from the caller's
    request. ``seat`` is optional only because seat-map reads (/seats) don't
    target a specific seat; reservations always pass one.
    """
    date_formatted = f"{date[8:10]}-{date[5:7]}-{date[:4]}"
    search_url = (
        f"{settings.base_url}/passagem-de-onibus/"
        f"?origin={origin_id}&destination={destination_id}"
        f"&date={date_formatted}&isStudent=false&isPCD=false&searchValidDay=true"
    )
    return RouteParams(
        origin_id=origin_id,
        destination_id=destination_id,
        date=date,
        departure=departure,
        seat=seat,
        date_formatted=date_formatted,
        search_url=search_url,
    )


def resolve_search_params(
    origin_id: str, destination_id: str, date: str, search_url: str
) -> RouteParams:
    """Frozen params for a URL search; navigates the user's exact URL.

    ``departure`` / ``seat`` are unused for search (no specific seat).
    """
    date_formatted = f"{date[8:10]}-{date[5:7]}-{date[:4]}"
    return RouteParams(
        origin_id=origin_id,
        destination_id=destination_id,
        date=date,
        departure="",
        seat="",
        date_formatted=date_formatted,
        search_url=search_url,
    )


# ─────────────────────────────────────────────────────────────────
# Single flow execution (the original run_flow body)
# ─────────────────────────────────────────────────────────────────
def _stimulate_fingerprint(page: Page, telemetry: TelemetryWatcher) -> None:
    log.info("[step 4] stimulating fingerprint generation")
    for _ in range(random.randint(2, 4)):
        stochastic_idle(page, "fingerprint_stimulus")
    try:
        canvas = page.locator("canvas, svg").first
        if canvas.count():
            box = canvas.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
                jitter(600, 1200)
    except Exception:  # FIX (bug 2)
        pass
    if not telemetry.seen:
        telemetry.wait_for(timeout=15.0)


def _execute_flow(playwright, params: RouteParams) -> tuple[int, dict | None]:
    """Run the 7-step booking flow once. Returns (exit_code, trip_dict | None)."""
    start = time.monotonic()
    ctx = build_context(playwright)
    page = ctx.new_page()
    telemetry = TelemetryWatcher(start_time=start)
    page.on("response", telemetry.on_response)

    try:
        open_search_page(page, telemetry, params)              # Step 1
        trip = resolve_trip(page, params)                      # Step 2

        if not check_seat_availability(trip["seatMap"], params):  # Step 3
            log.error(f"[step 3] seat {params.seat} already locked. Increase task interval.")
            return 1, None

        _stimulate_fingerprint(page, telemetry)                # Step 4 (pre)
        if not lock_seat(page, trip, params):                  # Step 4
            log.error("[step 4] failed to lock seat")
            return 1, None

        proceed_to_checkout(page, telemetry)                   # Step 5
        wait_for_lock_confirmation(page, trip, params)         # Step 6

        locked = confirm_seat_locked(page, trip, params)       # Step 7
        if locked:
            log.info(f"[result] seat {params.seat} UNAVAILABLE — lock confirmed")
            return 0, trip
        if "checkout" in page.url.lower() or "finalizar" in page.url.lower():
            log.info(f"[result] seat {params.seat} likely locked (checkout reached)")
            return 0, trip
        log.warning(f"[result] seat {params.seat} lock unconfirmed")
        return 1, None

    except RuntimeError as exc:
        log.error(f"[flow error] {exc}")
        return 2, None
    except Exception as exc:
        log.exception(f"[fatal] {exc}")
        return 2, None
    finally:
        try:
            ctx.close()
        except Exception:  # FIX (bug 2)
            pass


# ─────────────────────────────────────────────────────────────────
# Public synchronous entrypoints
# ─────────────────────────────────────────────────────────────────
def run_flow(
    params: RouteParams, reservation_id: str | None = None
) -> tuple[int, dict | None]:
    """
    Self-contained flow runner (manages Playwright + profile + error-retry).
    Safe to call inside a thread-pool executor; must NOT run on the event loop.

    Returns ``(exit_code, trip_dict | None)`` — the trip dict is present only on
    success (exit 0) so callers can compute the departure datetime.

    When ``reservation_id`` is supplied, every log line this flow emits is also
    written to a dedicated ``<log dir>/<id>.log`` for easy per-booking tracing.
    """
    with reservation_log(reservation_id):
        log.info("  BUS BOOKER")
        log.info(f"  From: {params.origin_id}  To: {params.destination_id}")
        log.info(f"  Date: {params.date}  |  Time: {params.departure}  |  Seat: {params.seat}")
        log.info(f"  URL:  {params.search_url}")

        if not _profile_looks_valid(settings.user_data_dir):
            log.warning("[main] profile missing — resetting")
            reset_profile(settings.user_data_dir)

        with sync_playwright() as pw:
            code, trip = _execute_flow(pw, params)
            if code == 2:
                log.warning("[main] flow error — resetting and retrying")
                reset_profile(settings.user_data_dir)
                jitter(2000, 4000)
                code, trip = _execute_flow(pw, params)
                if code == 2:
                    log.error("[main] flow error on retry — giving up")

        log.info(f"[main] exit({code})")
        return code, trip


def fetch_seat_map(params: RouteParams) -> tuple[list[SeatInfo], list[dict]]:
    """
    Playwright-free seat map fetch: HTML parse + direct BusDetails call.
    No browser navigation needed — the search page is server-rendered.

    Returns ``(seats, decks)`` — the flat seat list (for counts) and the full grid
    (decks→rows→cells) that mirrors mobifacil's own render for the seat picker.
    """
    import httpx
    from .htmlsearch import (
        build_bus_details_url,
        build_seat_decks,
        fetch_bus_details,
        fetch_lsservicos,
        filter_trips_by_date,
    )

    # Use a single client so cookies from the HTML fetch carry over to BusDetails.
    with httpx.Client(timeout=25, follow_redirects=True) as client:
        lsservicos = fetch_lsservicos(params.search_url, client=client)
        if not lsservicos:
            raise RuntimeError("no trips found in search page HTML")

        lsservicos = filter_trips_by_date(lsservicos, params.date)
        if not lsservicos:
            raise RuntimeError("no more trips for this date")

        def _dep_hour(trip: dict) -> str:
            saida = trip.get("saida", "")       # "02/06/2026 05:50"
            return saida.rsplit(" ", 1)[-1] if " " in saida else saida

        matching = next((t for t in lsservicos if _dep_hour(t) == params.departure), None)
        if not matching:
            available = [_dep_hour(t) for t in lsservicos]
            raise RuntimeError(f"departure {params.departure} not in search results {available}")

        url = build_bus_details_url(matching, params.date)
        bus_data = fetch_bus_details(url, client=client)

    if not bus_data:
        raise RuntimeError("BusDetails fetch failed")

    trips = bus_data.get("details", {}).get("trip", [])
    if not trips:
        raise RuntimeError("BusDetails returned no trip data")

    # trips[0] is the (only) bus for direct routes; trips[1] is a SECOND BUS in a
    # connection trip — not a second floor. Double-decker buses keep all seats in
    # trips[0].seatMap; decks are split on the EMPTY-row dividers (build_seat_decks),
    # not the unreliable per-seat z field.
    seat_map = trips[0].get("seatMap", [])
    return parse_seat_map(seat_map), build_seat_decks(seat_map)


def search_trips(params: RouteParams) -> list[dict]:
    """
    Playwright-free search: HTML parse only — no BusDetails calls needed.
    lsServicos already contains price, times, company, class, available seat
    count and duration. Individual seat maps belong to /seats, not /search.
    """
    from .htmlsearch import fetch_lsservicos, filter_trips_by_date, lsservicos_to_search_dict

    lsservicos = fetch_lsservicos(params.search_url)
    if not lsservicos:
        return []

    lsservicos = filter_trips_by_date(lsservicos, params.date)
    if not lsservicos:
        log.info("[search] mobifacil returned next-day data — no trips for requested date")
        return []

    results = [lsservicos_to_search_dict(t) for t in lsservicos]
    log.info(f"[search] {len(results)} trips from HTML")
    return results
