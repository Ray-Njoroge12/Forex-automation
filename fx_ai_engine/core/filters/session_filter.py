"""Session/liquidity filter — only trade during London and New York hours."""

from __future__ import annotations

from datetime import datetime, timezone

# (start_hour_utc_inclusive, end_hour_utc_exclusive)
SESSIONS_UTC: dict[str, tuple[int, int]] = {
    "london": (7, 16),    # 07:00–16:00 UTC
    "newyork": (13, 21),  # 13:00–21:00 UTC
}


def get_active_session(now_utc: datetime) -> str | None:
    """Return the name of the active session, or None if outside all sessions."""
    hour = now_utc.hour
    for name, (start, end) in SESSIONS_UTC.items():
        if start <= hour < end:
            return name
    return None


def is_tradeable_session(now_utc: datetime) -> bool:
    """Return True if *now_utc* falls within London or New York hours."""
    return get_active_session(now_utc) is not None
