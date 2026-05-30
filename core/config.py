"""
Centralised configuration for the bus-reserver service.

All previously-hardcoded values from the original script are exposed here as
``pydantic-settings`` fields (overridable via environment / ``.env``) with the
same defaults. Static API paths and the named city constants live as plain
module-level constants.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# ─────────────────────────────────────────────────────────────────
# Named city constants (exposed for callers / route defaults)
# ─────────────────────────────────────────────────────────────────
ARARAQUARA_ID: str = "19052"
SAO_CARLOS_ID: str = "19058"
SAO_PAULO_ID: str = "21787"

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

    # ---- route defaults ----
    origin_id: str = SAO_CARLOS_ID
    destination_id: str = SAO_PAULO_ID
    date: str = "2026-05-28"  # yyyy-mm-dd
    target_departure: str = "00:00"
    target_seat: str = "00"

    # ---- browser / site ----
    base_url: str = "https://mobifacil.com.br"
    user_data_dir: str = "./browser_profile"
    headless: bool = False

    # ---- flow tuning ----
    max_retries: int = 1
    wait_after_lock: int = 60  # seconds the lock is held before confirmation

    # ---- scheduler ----
    scheduler_interval: int = 21  # minutes

    # ---- logging ----
    log_file: str = "core/logs/app.log"
    log_max_bytes: int = 5_242_880
    log_backup_count: int = 3


# Single shared settings instance imported across the codebase.
settings = Settings()


def get_settings() -> Settings:
    """FastAPI-friendly accessor (kept tiny for dependency injection / tests)."""
    return settings
