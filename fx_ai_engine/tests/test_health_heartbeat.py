"""The engine heartbeat must be timezone-aware so the watchdog can detect a hung engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import health


def test_heartbeat_is_timezone_aware_utc():
    health.update_health_status(status="running")
    stamp = datetime.fromisoformat(health._health_status["last_heartbeat"])
    assert stamp.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 5


def test_watchdog_flags_old_heartbeat_as_stale():
    import watchdog

    old = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    assert watchdog._is_health_stale({"last_heartbeat": old}) is True
    assert watchdog._is_health_stale({"last_heartbeat": fresh}) is False
