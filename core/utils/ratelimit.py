"""
Lightweight, dependency-free abuse protection for the browser-driven endpoints
(/seats, /search, /reservations).

Two independent guards, each a no-op when its setting is 0:

* **Per-client-IP fixed-window rate limit** (fairness / abuse) → 429.
* **Global queue cap** on concurrent browser-bound requests (survival) → 503,
  so requests fail fast instead of piling up behind the single browser and
  hammering the upstream site into a ban.

Single worker, single event loop: the counters are only touched between awaits,
so plain ``int``/``dict`` access is atomic here and needs no locking. Client IPs
come from the trusted-proxy-aware resolver, so they cannot be spoofed past nginx.
"""
from __future__ import annotations

import time
from typing import AsyncIterator, Dict, Tuple

from fastapi import HTTPException, Request

from ..config import settings
from .net import client_info

_WINDOW_SECONDS = 60.0
_PRUNE_THRESHOLD = 2048  # sweep stale IP buckets once the table grows past this

# client-ip -> (window_start_monotonic, count_in_window)
_hits: Dict[str, Tuple[float, int]] = {}
# concurrent browser-bound requests currently queued or running
_in_flight = 0


def _rate_limited(ip: str, limit: int) -> bool:
    now = time.monotonic()
    start, count = _hits.get(ip, (now, 0))
    if now - start >= _WINDOW_SECONDS:  # window rolled over → reset
        start, count = now, 0
    if count >= limit:
        _hits[ip] = (start, count)
        return True
    _hits[ip] = (start, count + 1)
    if len(_hits) > _PRUNE_THRESHOLD:
        cutoff = now - _WINDOW_SECONDS
        for stale in [k for k, (s, _) in _hits.items() if s < cutoff]:
            _hits.pop(stale, None)
    return False


async def browser_guard(request: Request) -> AsyncIterator[None]:
    """FastAPI dependency for browser-driven endpoints.

    Apply with ``dependencies=[Depends(browser_guard)]``. Uses a ``yield`` so the
    in-flight counter is released after the response, whatever the outcome.
    """
    global _in_flight
    info = getattr(request.state, "client", None) or client_info(request)

    limit = settings.rate_limit_per_min
    if limit > 0 and _rate_limited(info.ip, limit):
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded — slow down",
            headers={"Retry-After": "60"},
        )

    cap = settings.max_flow_queue
    if cap > 0 and _in_flight >= cap:
        raise HTTPException(
            status_code=503,
            detail="server busy (browser queue full) — retry shortly",
            headers={"Retry-After": "30"},
        )

    _in_flight += 1
    try:
        yield
    finally:
        _in_flight -= 1
