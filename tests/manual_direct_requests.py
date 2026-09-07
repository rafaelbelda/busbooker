#!/usr/bin/env python
"""
Smoke-test the Playwright-free search and seats paths.

Tests two things:
  1. fetch_lsservicos — HTML GET + parse (no browser needed)
  2. fetch_bus_details — direct BusDetails call for one trip (for /seats path)

Requires ADMIN_PASSWORD in .env.

Usage (from project root):
  python tests/test_direct_requests.py --url "https://mobifacil.com.br/passagem-de-onibus/..."
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

from core.services.htmlsearch import (
    build_bus_details_url,
    fetch_bus_details,
    fetch_lsservicos,
    lsservicos_to_search_dict,
)


def run(search_url: str) -> int:
    print(f"\n=== 1. Fetch lsServicos from HTML ===")
    t0 = time.monotonic()
    try:
        trips = fetch_lsservicos(search_url)
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1
    html_elapsed = time.monotonic() - t0
    print(f"  {len(trips)} trips in {html_elapsed:.1f}s")
    if not trips:
        print("FAIL: no trips returned")
        return 1

    for i, t in enumerate(trips):
        d = lsservicos_to_search_dict(t)
        print(f"  trip {i}: {d['departure']}→{d['arrival']}  {d['company']}"
              f"  R${d['price']}  {d['available_seats']} seats  {d['duration']}")

    print(f"\n=== 2. Fetch BusDetails for first trip (seats path) ===")
    t1 = time.monotonic()
    date = trips[0].get("dataCorrida", "")
    if not date:
        print("SKIP: dataCorrida missing from first trip")
        return 0

    url = build_bus_details_url(trips[0], date)
    data = fetch_bus_details(url)
    bd_elapsed = time.monotonic() - t1
    if not data:
        print(f"FAIL: BusDetails returned None in {bd_elapsed:.2f}s")
        return 1

    bd_trips = data.get("details", {}).get("trip", [])
    seat_count = sum(
        1 for row in (bd_trips[0].get("seatMap", []) if bd_trips else [])
        for s in (row if isinstance(row, list) else [])
        if isinstance(s, dict) and s.get("disponivel")
    )
    has_2f = bd_trips[0].get("hasSecondFloor", False) if bd_trips else False
    print(f"  BusDetails OK in {bd_elapsed:.2f}s — {seat_count} available seats"
          f"  double-decker={has_2f}")

    print(f"\nTotal: {html_elapsed + bd_elapsed:.1f}s  (old estimate: >{len(trips) * 20 + 10:.0f}s)")
    print("PASS")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Smoke-test htmlsearch paths")
    p.add_argument("--url", required=True, help="Mobifacil passagem-de-onibus search URL")
    args = p.parse_args()
    sys.exit(run(args.url))
