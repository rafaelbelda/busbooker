"""Time helpers — departure datetime computation in the route's local timezone."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# mobifacil times are local to Brazil; the site/route timezone is São Paulo.
SAO_PAULO_TZ = ZoneInfo("America/Sao_Paulo")


def compute_departure_datetime(date_str: str, departure_hhmm: str) -> datetime:
    """
    Combine ``yyyy-mm-dd`` + ``HH:MM`` interpreted in America/Sao_Paulo and
    return a UTC-aware datetime.

    Example: ``"2026-05-28"`` + ``"00:00"`` -> ``2026-05-28T03:00:00+00:00``.
    """
    local_dt = datetime.strptime(f"{date_str} {departure_hhmm}", "%Y-%m-%d %H:%M")
    aware_local = local_dt.replace(tzinfo=SAO_PAULO_TZ)
    return aware_local.astimezone(timezone.utc)
