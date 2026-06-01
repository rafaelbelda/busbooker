#!/usr/bin/env python
"""
Intercept every JSON response during a mobifacil search page load to discover
whether a direct search/listing API endpoint exists — one we could call without
navigating a browser at all.

Also dumps page HTML so we can tell if trips are server-rendered or AJAX-driven.

Requires ADMIN_PASSWORD in .env.

Usage (from project root):
  python tests/explore_api.py --url "https://mobifacil.com.br/passagem-de-onibus/..."

Look for "*** TRIP DATA" lines — the URL next to them is the search API.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, ".")

from playwright.sync_api import Response, sync_playwright

from core.services.browser import jitter


_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR', 'pt', 'en-US', 'en'] });
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""

# Noise we don't care about.
_SKIP = [
    "fingerprint", "analytics", "gtm", "googletagmanager", "facebook",
    "hotjar", "clarity", "doubleclick", "google-analytics", "segment",
    ".png", ".jpg", ".gif", ".webp", ".svg", ".css", ".woff", ".ico",
]

# Tight keywords: only things that definitely mean bus trip data.
_TRIP_HINTS = ["serviceId", "departureHour", "seatMap", "BusDetails", "horarioBus"]


def _skip(url: str) -> bool:
    return any(p in url.lower() for p in _SKIP)


def _is_json(response: Response) -> bool:
    ct = (response.headers.get("content-type") or "").lower()
    return "json" in ct


def run(search_url: str) -> int:
    all_json: list[dict] = []
    trip_responses: list[dict] = []

    def on_response(resp: Response) -> None:
        if _skip(resp.url) or not _is_json(resp):
            return
        try:
            body = resp.json()
        except Exception:
            return
        text = json.dumps(body)
        is_trip = any(h in text for h in _TRIP_HINTS)
        entry = {"url": resp.url, "status": resp.status, "body_len": len(text),
                 "preview": text[:400], "is_trip": is_trip}
        all_json.append(entry)
        if is_trip:
            trip_responses.append(entry)
            print(f"  *** TRIP DATA [{resp.status}] {resp.url[:110]}")
            print(f"      {text[:250]}")
        else:
            path = urlparse(resp.url).path
            print(f"  [{resp.status}] {path[:90]}  ({len(text)}b)")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir="./browser_profile_explore",
            headless=True,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage"],
            ignore_default_args=["--enable-automation"],
        )
        ctx.add_init_script(_INIT_SCRIPT)
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            print(f"\nLoading: {search_url[:110]}")
            t0 = time.monotonic()
            page.goto(search_url, wait_until="load", timeout=60_000)
            print(f"load event: {time.monotonic() - t0:.1f}s\n")

            cards_found = False
            try:
                page.wait_for_selector(".listTripsCard", timeout=30_000)
                cards_found = True
                print(f"\n.listTripsCard appeared at {time.monotonic() - t0:.1f}s")
            except Exception:
                print(f"\n[WARN] .listTripsCard never appeared after {time.monotonic() - t0:.1f}s")

            jitter(2000, 3000)

            # Always dump a slice of the HTML so we can tell what's on the page.
            html = page.content()
            has_list_html = "listTripsCard" in html
            has_no_trips = any(x in html.lower() for x in
                               ["nenhuma viagem", "sem viagens", "não encontramos", "no trips"])
            print(f"\nHTML size: {len(html)} bytes")
            print(f"  'listTripsCard' in HTML: {has_list_html}")
            print(f"  'no trips' message:       {has_no_trips}")

            # Print a slice around any listTripsCard occurrence.
            if has_list_html:
                idx = html.find("listTripsCard")
                print(f"\n  HTML context around .listTripsCard:")
                print(f"  {html[max(0,idx-80):idx+200]!r}")
            else:
                # Show enough of the body to diagnose what's there instead.
                body_start = html.find("<body")
                snippet = html[body_start:body_start + 600] if body_start >= 0 else html[:600]
                print(f"\n  Page body snippet:\n  {snippet!r}")

        finally:
            ctx.close()

    print(f"\n{'='*60}")
    print(f"JSON responses captured: {len(all_json)}")
    print(f"Trip-data hits:          {len(trip_responses)}")

    if trip_responses:
        print("\n--- Trip-data endpoints ---")
        for r in trip_responses:
            print(f"\n  URL:    {r['url']}")
            print(f"  Status: {r['status']}   Body: {r['body_len']}b")
            print(f"  Body:   {r['preview']}")
        print("\nNext: call these URLs with page.request.get() to skip page.goto().")
    else:
        print("\nNo trip-data XHR found.")
        print("If 'listTripsCard' was in the HTML, trips are server-rendered — parse with httpx.")
        print("If not, check the page body snippet above for a captcha or redirect clue.")

    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Discover mobifacil search API endpoints")
    p.add_argument("--url", required=True, help="Mobifacil passagem-de-onibus search URL")
    args = p.parse_args()
    sys.exit(run(args.url))
