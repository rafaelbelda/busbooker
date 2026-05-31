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
    max_retries: int = 1
    wait_after_lock: int = 60  # seconds the lock is held before confirmation

    # ---- scheduler ----
    scheduler_interval: int = 21  # minutes

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
