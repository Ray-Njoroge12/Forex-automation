"""Session/liquidity filter — only trade during London and New York hours."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SessionDefinition:
    timezone_name: str
    start_hour_local: int
    end_hour_local: int


# Local-market hours. ZoneInfo converts them to UTC, including DST transitions.
# These preserve the original intended windows: London 07:00–16:00 local and
# New York 08:00–16:00 local.
SESSIONS: dict[str, SessionDefinition] = {
    "london": SessionDefinition("Europe/London", 7, 16),
    "newyork": SessionDefinition("America/New_York", 8, 16),
}


def _as_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("session evaluation requires a timezone-aware UTC timestamp")
    return now.astimezone(timezone.utc)


def get_active_session(now_utc: datetime) -> str | None:
    """Return the name of the active session, or None if outside all sessions."""
    now = _as_utc(now_utc)
    for name, definition in SESSIONS.items():
        local_hour = now.astimezone(ZoneInfo(definition.timezone_name)).hour
        if definition.start_hour_local <= local_hour < definition.end_hour_local:
            return name
    return None


def is_tradeable_session(now_utc: datetime) -> bool:
    """Return True if *now_utc* falls within London or New York hours."""
    return get_active_session(now_utc) is not None
