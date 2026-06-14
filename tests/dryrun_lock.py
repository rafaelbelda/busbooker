#!/usr/bin/env python
"""
Dry-run the NEW authoritative lock path (Phase 1-2).

Unlike capture_lockseat.py (which tried to trigger mobifacil's own JS by clicking
a seat — impossible on the <canvas> map), this drives the app's real flow:

    open_search_page → resolve_trip → check_seat_availability → lock_seat

i.e. it POSTs LockSeat directly via core.services.seat.lock_seat and prints the
returned seatUUID. A success looks like:

    [dryrun] LOCKED ✓  seatUUID=<token>

It does NOT touch the reservation store or schedule any re-lock — it just proves
the lock + confirmation works end to end. Pick a route/seat that is AVAILABLE
(green) and a departure later than the current time (otherwise mobifacil rolls
to the next day).

Usage (from project root, needs ADMIN_PASSWORD in .env):
  python tests/dryrun_lock.py \
      --origin -3 --destination 19052 --date 2026-06-14 \
      --departure 23:30 --seat 5 [--headed]
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright

from core.config import settings
from core.services.browser import TelemetryWatcher, build_context
from core.services.checkout import corroborate_lock, proceed_to_checkout
from core.services.flow import _stimulate_fingerprint, resolve_route_params
from core.services.seat import check_seat_availability, lock_seat
from core.services.trip import open_search_page, resolve_trip


def run(origin: str, destination: str, date: str, departure: str,
        seat: str, headed: bool) -> int:
    if headed:
        settings.headless = False  # type: ignore[misc]

    params = resolve_route_params(origin, destination, date, departure, seat)

    with sync_playwright() as pw:
        ctx = build_context(pw)
        page = ctx.new_page()
        telemetry = TelemetryWatcher(start_time=time.monotonic())
        page.on("response", telemetry.on_response)
        try:
            print(f"\n[dryrun] {origin}->{destination} {date} {departure} seat {seat}")
            open_search_page(page, telemetry, params)                 # Step 1
            trip = resolve_trip(page, params)                         # Step 2
            print(f"[dryrun] trip resolved — serviceId={trip.get('serviceId')}")

            avail = check_seat_availability(trip["seatMap"], params)  # Step 3
            print(f"[dryrun] seat {seat} available={avail}")
            if not avail:
                print("[dryrun] seat not available — pick a green seat. Aborting.")
                return 1

            _stimulate_fingerprint(page, telemetry)                   # Step 4 (pre)
            seat_uuid = lock_seat(page, trip, params)                 # Step 4
            if not seat_uuid:
                print("\n[dryrun] LOCK FAILED ✗  (LockSeat declined — see log above)")
                return 1

            print(f"\n[dryrun] LOCKED ✓  seatUUID={seat_uuid}")

            # Best-effort, exactly as the flow does it (non-fatal corroboration).
            try:
                proceed_to_checkout(page, telemetry)                  # Step 5
            except Exception as exc:
                print(f"[dryrun] checkout nav skipped (non-fatal): {exc!r}")
            corroborated = corroborate_lock(page, trip, params)       # Step 6
            print(f"[dryrun] BusDetails corroboration: {corroborated} "
                  "(None/False just means cache lag — the seatUUID is the proof)")
            return 0
        finally:
            ctx.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Dry-run the direct LockSeat path")
    p.add_argument("--origin", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--date", required=True, help="yyyy-mm-dd")
    p.add_argument("--departure", required=True, help="HH:MM (use a time later than now)")
    p.add_argument("--seat", required=True)
    p.add_argument("--headed", action="store_true", help="run with a visible browser")
    args = p.parse_args()
    sys.exit(run(args.origin, args.destination, args.date,
                 args.departure, args.seat, args.headed))
