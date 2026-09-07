"""
Centralised configuration for the bus-reserver service.

Operational, non-route settings are exposed here as ``pydantic-settings`` fields
(overridable via environment / ``.env``). Route values (origin, destination,
date, departure, seat) are **never** configured here — they always come from an
explicit user request, so the service can never book a default route on its own.
Static API paths live as plain module-level constants.
"""
from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# ─────────────────────────────────────────────────────────────────
# Static demandware endpoint paths (not route-specific)
# ─────────────────────────────────────────────────────────────────
BUS_DETAILS_PATH: str = "/on/demandware.store/Sites-Mobifacil-Site/pt_BR/BusDetails-BusDetails"
LOCK_SEAT_PATH: str = "/on/demandware.store/Sites-Mobifacil-Site/pt_BR/LockSeat-LockSeat"
CHECKOUT_PATH: str = "/on/demandware.store/Sites-Mobifacil-Site/pt_BR/Checkout-Begin"
FINGERPRINT_PATTERN: str = "fingerprint/high/"


class Settings(BaseSettings):
    """Environment-driven configuration (case-insensitive env var matching)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- browser / site ----
    base_url: str = "https://mobifacil.com.br"
    user_data_dir: str = "./browser_profile"
    headless: bool = False

    # ---- admin ----
    # HTTP Basic password for /admin/* (username is fixed "admin").
    # Required, no default — see the startup check below.
    admin_password: Optional[str] = None

    # ---- flow tuning ----
    # Attempts (not *re*-tries) for retry-wrapped browser steps. Was 1, which made
    # the retry wrapper a no-op for open_search_page and proceed_to_checkout: a
    # transient page-load failure went straight to exit 2, costing a profile reset
    # and a whole second flow. One in-place retry is far cheaper than that.
    max_retries: int = 2

    # ---- scheduler ----
    scheduler_interval: int = 20  # minutes
    # Stop re-locking this many minutes BEFORE departure. mobifacil delists a trip
    # some minutes before it leaves (observed: still listed at T-22min, gone by
    # T-90s), and a re-lock that lands after delisting cannot succeed — it used to
    # burn a full flow, a profile reset and a retry to discover that. Re-locking in
    # the last few minutes has no value anyway: the passenger is already boarding.
    relock_stop_minutes_before_departure: int = 15

    # ---- persistence ----
    # SQLite file holding reservations so they (and their re-lock jobs) survive a
    # restart. Use ":memory:" to disable durability (e.g. in tests).
    reservation_db: str = "core/data/reservations.db"

    # ---- abuse protection (matters once the browser endpoints face the public) ----
    # Per-client-IP request cap per minute on the browser-driven endpoints
    # (/seats, /search, /reservations). 0 disables it (default — tailnet-only).
    rate_limit_per_min: int = 0
    # Max concurrent (queued + running) browser-bound requests before new ones get
    # a fast 503 instead of piling up behind the single browser. 0 disables it.
    max_flow_queue: int = 8

    # ---- request traceability ----
    # Comma-separated IPs/CIDRs of reverse proxies whose forwarded headers
    # (X-Real-IP / X-Forwarded-For) we trust. Loopback is always trusted, so the
    # default nginx-on-localhost setup needs nothing here. Set this only if a
    # proxy reaches the app from a non-loopback address.
    trusted_proxies: str = ""

    # ---- logging ----
    log_file: str = "core/logs/app.log"
    log_max_bytes: int = 5_242_880
    log_backup_count: int = 3
    # Per-reservation log files (one <id>.log per reservation, appended across
    # the initial lock and every re-lock) — invaluable for tracing a single booking.
    reservation_log_dir: str = "core/logs/reservations"


# Single shared settings instance imported across the codebase.
settings = Settings()

# ADMIN_PASSWORD is required and has no default — fail fast at startup (import
# time) with a clear message rather than 500ing later on the first /admin call.
if not settings.admin_password:
    raise ValueError(
        "ADMIN_PASSWORD is required and has no default. Set the ADMIN_PASSWORD "
        "environment variable (or add it to .env) before starting the service."
    )


def get_settings() -> Settings:
    """FastAPI-friendly accessor (kept tiny for dependency injection / tests)."""
    return settings
