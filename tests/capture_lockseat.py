#!/usr/bin/env python
"""
Capture the REAL LockSeat POST that mobifacil's own front-end JS sends.

Why this exists
---------------
Our API fallback (`core/services/seat.py::lock_seat_api`) is failing on every
reservation with a server-side error:

    LockSeatModel.js:115  "Unexpected token: u"

That string is Demandware/Rhino's signature for ``JSON.parse(undefined)`` — the
server is parsing a form field that is **absent** from our POST. To fix it
against ground truth (instead of guessing field names), this script drives a
real browser session through steps 1-2 of the normal flow, lands on the seat
map, clicks a seat so mobifacil's OWN JS issues the LockSeat request, and dumps
the exact outbound form body + the response.

Compare the captured field set against ``_build_lock_payload`` to see:
  * which field name carries the seat-array JSON (seatMap vs seatsWithPrice),
  * what `infoConnection` (and other "empty" fields we drop) really contain,
  * any field we omit entirely.

Usage (from project root, needs ADMIN_PASSWORD in .env like the app):
  python tests/capture_lockseat.py \
      --origin -3 --destination 19052 --date 2026-06-13 \
      --departure 09:30 --seat 5 [--headed]

Pick a route/seat that is actually AVAILABLE (green) — locking needs a free seat.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from typing import Optional

sys.path.insert(0, ".")

from playwright.sync_api import Request, Response, sync_playwright

from core.config import LOCK_SEAT_PATH, settings
from core.services.browser import build_context, jitter
from core.services.flow import resolve_route_params
from core.services.seat import (
    find_clickable_seat_in_ui,
    _click_proceed_button,
    _PROCEED_SELECTORS,
)
from core.services.trip import open_search_page, resolve_trip, _navigate_via_url
from core.services.browser import TelemetryWatcher


def _decode_form(body: Optional[str]) -> dict[str, str]:
    """Parse an x-www-form-urlencoded body into a flat {field: value} dict."""
    if not body:
        return {}
    # keep_blank_values so we SEE fields sent as "" (vs fields omitted entirely).
    pairs = urllib.parse.parse_qsl(body, keep_blank_values=True)
    return dict(pairs)


def _classify(value: str) -> str:
    """Label a form value: JSON object/array, empty, or scalar (with a preview)."""
    if value == "":
        return "EMPTY STRING"
    stripped = value.strip()
    if stripped[:1] in "[{":
        try:
            parsed = json.loads(stripped)
            kind = "array" if isinstance(parsed, list) else "object"
            return f"JSON {kind} (len={len(parsed)})"
        except json.JSONDecodeError:
            return f"LOOKS-LIKE-JSON BUT INVALID :: {value[:80]!r}"
    preview = value if len(value) <= 80 else value[:77] + "..."
    return f"scalar :: {preview!r}"


def _dump_request(req: Request) -> None:
    print("\n" + "=" * 72)
    print("REAL LockSeat REQUEST captured")
    print("=" * 72)
    print(f"  method : {req.method}")
    print(f"  url    : {req.url}")
    ct = req.headers.get("content-type", "")
    print(f"  content-type: {ct}")

    body = req.post_data
    fields = _decode_form(body)
    if not fields:
        # Not form-encoded — show raw (could be JSON body).
        print("\n  [not form-encoded — raw body follows]")
        print(f"  {body!r}")
        return

    print(f"\n  {len(fields)} form fields (NAME → kind):")
    for name, value in fields.items():
        print(f"    - {name:<26} {_classify(value)}")

    # Highlight the fields most relevant to the bug.
    print("\n  --- fields relevant to LockSeatModel.js:115 JSON.parse ---")
    for key in ("seatMap", "seatsWithPrice", "infoConnection", "connectionId",
                "offerId", "upsellOriginalPrice"):
        if key in fields:
            print(f"    PRESENT  {key:<20} {_classify(fields[key])}")
        else:
            print(f"    ABSENT   {key}")

    # Persist the full body for offline diffing.
    out = "tests/_lockseat_capture.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"url": req.url, "content_type": ct, "fields": fields},
                  fh, ensure_ascii=False, indent=2)
    print(f"\n  full field dump written to {out}")


def run(origin: str, destination: str, date: str, departure: str,
        seat: str, headed: bool) -> int:
    params = resolve_route_params(origin, destination, date, departure, seat)

    captured_req: dict = {}
    captured_resp: dict = {}

    def on_request(req: Request) -> None:
        if LOCK_SEAT_PATH in req.url and req.method == "POST" and not captured_req:
            captured_req["req"] = req
            _dump_request(req)

    def on_response(resp: Response) -> None:
        if LOCK_SEAT_PATH in resp.url and not captured_resp:
            try:
                captured_resp["body"] = resp.json()
            except Exception:
                captured_resp["body"] = {"_raw": (resp.text() or "")[:400]}
            print("\n  --- LockSeat RESPONSE ---")
            print(f"  {json.dumps(captured_resp['body'], ensure_ascii=False)[:600]}")

    if headed:
        settings.headless = False  # type: ignore[misc]

    with sync_playwright() as pw:
        ctx = build_context(pw)
        page = ctx.new_page()
        page.on("request", on_request)
        page.on("response", on_response)
        telemetry = TelemetryWatcher(start_time=time.monotonic())
        page.on("response", telemetry.on_response)

        try:
            print(f"\n[capture] resolving trip {origin}->{destination} "
                  f"{date} {departure} seat {seat}")
            open_search_page(page, telemetry, params)
            trip = resolve_trip(page, params)
            print(f"[capture] trip resolved — serviceId={trip.get('serviceId')}")

            # resolve_trip is intercept-only: it captured the BusDetails XHR but
            # the page is still on the trip LIST, so seat spans are hidden. Force
            # navigation to the seat-selection page so the map renders and a seat
            # click actually fires mobifacil's LockSeat.
            print("[capture] navigating to seat-selection page")
            try:
                _navigate_via_url(page, params)
            except RuntimeError as exc:
                print(f"[capture] navigation fallback failed: {exc!r}")
            jitter(1500, 2500)

            # Let mobifacil's own JS fire LockSeat by clicking the seat in the UI.
            clicked = find_clickable_seat_in_ui(page, seat)
            if not clicked:
                # Click a VISIBLE seat label (padded or not) and, if needed, its
                # clickable ancestor — we just need SOME LockSeat to fire.
                print("[capture] DOM/SVG match failed — trying visible label click")
                for cand in (seat.zfill(2), seat, seat.lstrip("0")):
                    loc = page.get_by_text(cand, exact=True)
                    for i in range(loc.count()):
                        el = loc.nth(i)
                        try:
                            if not el.is_visible():
                                continue
                            el.scroll_into_view_if_needed(timeout=3000)
                            try:
                                el.click(timeout=4000)
                            except Exception:
                                # fall back to the clickable parent (the seat button)
                                el.locator("..").click(timeout=4000, force=True)
                            clicked = True
                            print(f"[capture] clicked seat label '{cand}'")
                            break
                        except Exception as exc:
                            print(f"[capture] label '{cand}' #{i} click failed: {exc!r}")
                    if clicked:
                        break

            if clicked:
                # Some flows only POST LockSeat after a 'Continuar' step.
                _click_proceed_button(page, _PROCEED_SELECTORS)

            if not clicked:
                print("[capture] could not click a seat — dumping seat-map DOM "
                      "so we can adjust the selector:")
                html = page.locator(
                    "canvas, svg, [class*='busMap'], [class*='seatmap'], "
                    "[class*='poltrona'], [class*='seat']"
                ).first
                try:
                    print(html.evaluate("el => el.outerHTML")[:1500])
                except Exception:
                    print("  (no seat-map container found)")

            # Give the lock request time to fire and the response to land.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not captured_req:
                page.wait_for_timeout(400)

            if not captured_req:
                print("\n[capture] NO LockSeat request observed. The seat click "
                      "may not have triggered it (canvas map, or lock happens on a "
                      "later 'Continuar' step). Re-run with --headed to watch, or "
                      "tell me what the seat map looks like.")
                return 2
            return 0
        finally:
            jitter(500, 1000)
            ctx.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Capture real mobifacil LockSeat POST")
    p.add_argument("--origin", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--date", required=True, help="yyyy-mm-dd")
    p.add_argument("--departure", required=True, help="HH:MM")
    p.add_argument("--seat", required=True)
    p.add_argument("--headed", action="store_true", help="run with a visible browser")
    args = p.parse_args()
    sys.exit(run(args.origin, args.destination, args.date,
                 args.departure, args.seat, args.headed))
