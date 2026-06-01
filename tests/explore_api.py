#!/usr/bin/env python
"""
Intercept every JSON response during a mobifacil search page load to discover
whether a direct search/listing API endpoint exists — one we could call without
navigating a browser at all.

Requires ADMIN_PASSWORD in .env.

Usage (from project root):
  python tests/explore_api.py --url "https://mobifacil.com.br/passagem-de-onibus/..."

Look for responses whose preview contains serviceId, departureHour, trip, etc.
That URL is the search API we want to call directly.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, ".")

from playwright.sync_api import Response, sync_playwright

from core.services.browser import build_context, jitter


# Noise we don't care about.
_SKIP = [
    "fingerprint", "analytics", "gtm", "googletagmanager", "facebook",
    "hotjar", "clarity", "doubleclick", "google-analytics", "segment",
    ".png", ".jpg", ".gif", ".webp", ".svg", ".css", ".woff", ".ico",
]

# Keywords that suggest a response contains trip/search data.
_TRIP_HINTS = ["serviceId", "departureHour", "seatMap", "BusDetails", "trip", "horario"]


def _skip(url: str) -> bool:
    low = url.lower()
    return any(p in low for p in _SKIP)


def _interesting_ct(response: Response) -> bool:
    ct = (response.headers.get("content-type") or "").lower()
    return "json" in ct


def run(search_url: str) -> int:
    all_responses: list[dict] = []
    trip_responses: list[dict] = []

    def on_response(resp: Response) -> None:
        if _skip(resp.url) or not _interesting_ct(resp):
            return
        try:
            body = resp.json()
        except Exception:
            return
        text = json.dumps(body)
        entry = {
            "url": resp.url,
            "status": resp.status,
            "body_len": len(text),
            "preview": text[:300],
            "is_trip_data": any(h in text for h in _TRIP_HINTS),
        }
        all_responses.append(entry)
        if entry["is_trip_data"]:
            trip_responses.append(entry)
            print(f"  *** TRIP DATA [{resp.status}] {resp.url[:100]}")
            print(f"      {text[:200]}")
        else:
            parsed = urlparse(resp.url)
            print(f"  [{resp.status}] {parsed.path[:80]}")

    with sync_playwright() as pw:
        ctx = build_context(pw)
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            print(f"\nLoading: {search_url[:100]}")
            t0 = time.monotonic()
            page.goto(search_url, wait_until="load", timeout=60_000)
            elapsed_load = time.monotonic() - t0
            print(f"load event: {elapsed_load:.1f}s\n")

            try:
                page.wait_for_selector(".listTripsCard", timeout=30_000)
                elapsed_cards = time.monotonic() - t0
                print(f"\n.listTripsCard appeared at {elapsed_cards:.1f}s")
            except Exception:
                print("\n[WARN] .listTripsCard never appeared")

            jitter(2000, 3000)  # let any trailing responses arrive
        finally:
            ctx.close()

    print(f"\n{'='*60}")
    print(f"Total JSON responses: {len(all_responses)}")
    print(f"Trip-data responses:  {len(trip_responses)}")

    if trip_responses:
        print("\n--- Trip-data endpoints ---")
        for r in trip_responses:
            print(f"\n  URL:    {r['url']}")
            print(f"  Status: {r['status']}  Body: {r['body_len']} bytes")
            print(f"  Body:   {r['preview']}")
        print("\nNext step: call one of these URLs with page.request.get() to skip page.goto().")
    else:
        print("\nNo direct trip-data API found. Trip list may be server-rendered in the HTML.")
        print("Try: page.content() after load and grep for .listTripsCard to confirm.")

    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Discover mobifacil search API endpoints")
    p.add_argument("--url", required=True, help="Mobifacil search URL")
    args = p.parse_args()
    sys.exit(run(args.url))
