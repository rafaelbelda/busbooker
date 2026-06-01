#!/usr/bin/env python
"""
Verify that page.request.get() on data-urlbusdetails URLs returns valid
BusDetails JSON without triggering bot detection.

Requires ADMIN_PASSWORD in .env (loaded by core.config at import time).

Usage (from project root):
  python tests/test_direct_requests.py --url "https://mobifacil.com.br/passagem-de-onibus/?origin=...&destination=...&date=dd-mm-yyyy"
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright

from core.config import settings
from core.services.browser import check_detection, jitter
from core.services.trip import _parse_all_trips
from core.utils.logger import log


def run(search_url: str) -> int:
    """Returns 0 on all-pass, 1 on any failure."""
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir="./browser_profile_test",
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
        page = ctx.new_page()
        try:
            print(f"\nLoading search page …")
            t0 = time.monotonic()
            page.goto(search_url, wait_until="load", timeout=60_000)
            check_detection(page, "search")
            page.wait_for_selector(".listTripsCard", timeout=30_000)
            page_load_s = time.monotonic() - t0
            print(f"  page load:  {page_load_s:.1f}s")

            cards = page.locator(".listTripsCard")
            urls: list[str] = []
            for i in range(cards.count()):
                card = cards.nth(i)
                cls = card.get_attribute("class") or ""
                if "soldOut" in cls:
                    continue
                u = card.get_attribute("data-urlbusdetails")
                if u:
                    urls.append(u if u.startswith("http") else settings.base_url + u)

            print(f"  available trips: {len(urls)}")
            if not urls:
                print("\nFAIL: no trip URLs found on search page")
                return 1

            passed = failed = 0
            total_req_s = 0.0
            for idx, url in enumerate(urls):
                t1 = time.monotonic()
                resp = page.request.get(url, timeout=15_000)
                elapsed = time.monotonic() - t1
                total_req_s += elapsed

                if not resp.ok:
                    print(f"  trip {idx}: HTTP {resp.status} in {elapsed:.2f}s  FAIL")
                    failed += 1
                    continue

                try:
                    data = resp.json()
                    trips = _parse_all_trips(data)
                    print(f"  trip {idx}: OK in {elapsed:.2f}s  ({len(trips)} trip(s) in payload)")
                    passed += 1
                except Exception as exc:
                    print(f"  trip {idx}: JSON error in {elapsed:.2f}s — {exc}  FAIL")
                    failed += 1

                jitter(300, 700)

            old_estimate = page_load_s + len(urls) * 20
            print(f"\nResults:     {passed} passed, {failed} failed")
            print(f"API time:    {total_req_s:.1f}s total for {len(urls)} trip(s)")
            print(f"Old (est.):  ~{old_estimate:.0f}s with page.goto()")
            print(f"Speedup:     ~{old_estimate / max(total_req_s, 0.1):.0f}×")
            return 0 if failed == 0 else 1
        finally:
            ctx.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Test direct BusDetails fetching via page.request.get()")
    p.add_argument("--url", required=True, help="Mobifacil passagem-de-onibus search URL")
    args = p.parse_args()
    sys.exit(run(args.url))
