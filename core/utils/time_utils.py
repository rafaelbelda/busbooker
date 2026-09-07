"""Time helpers — departure/arrival datetime computation in the route's local timezone."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def relock_cutoff(departure_dt: datetime, stop_minutes_before: int) -> datetime:
    """Instant after which re-locking a seat is pointless.

    The provider delists a trip shortly before departure, so a re-lock scheduled
    into that window cannot succeed; and holding a seat for a passenger who is
    already boarding buys nothing. Callers pass
    ``settings.relock_stop_minutes_before_departure`` — kept as an argument so this
    module stays free of config imports and trivially testable.
    """
    return departure_dt - timedelta(minutes=stop_minutes_before)


def compute_arrival_datetime(date_str: str, dep_hhmm: str, arr_hhmm: str) -> datetime:
    """
    Compute arrival datetime in UTC, handling overnight trips.

    If arr_hhmm ≤ dep_hhmm (clock minutes), the bus arrives the next calendar
    day in São Paulo time (e.g. dep 23:30, arr 05:15 → arrival on date+1).
    """
    dep_mins = int(dep_hhmm[:2]) * 60 + int(dep_hhmm[3:])
    arr_mins = int(arr_hhmm[:2]) * 60 + int(arr_hhmm[3:])
    arrival_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    if arr_mins <= dep_mins:
        arrival_date = arrival_date + timedelta(days=1)
    local_dt = datetime.strptime(f"{arrival_date} {arr_hhmm}", "%Y-%m-%d %H:%M")
    aware_local = local_dt.replace(tzinfo=SAO_PAULO_TZ)
    return aware_local.astimezone(timezone.utc)
