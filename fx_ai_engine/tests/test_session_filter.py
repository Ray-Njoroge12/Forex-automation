from __future__ import annotations

from datetime import datetime, timezone

from core.filters.session_filter import get_active_session, is_tradeable_session


def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, 10, hour, minute, 0, tzinfo=timezone.utc)


def test_london_open_is_tradeable() -> None:
    assert is_tradeable_session(_utc(7, 0)) is True


def test_one_minute_before_london_is_not_tradeable() -> None:
    assert is_tradeable_session(_utc(6, 59)) is False


def test_london_close_still_tradeable_via_ny() -> None:
    """16:00 UTC: London closes but New York (13:00-21:00) is still open."""
    assert is_tradeable_session(_utc(16, 0)) is True


def test_newyork_open_is_tradeable() -> None:
    assert is_tradeable_session(_utc(13, 0)) is True


def test_newyork_close_boundary_is_not_tradeable() -> None:
    """21:00 UTC is the exclusive end of New York — not tradeable."""
    assert is_tradeable_session(_utc(21, 0)) is False


def test_one_minute_before_newyork_close_is_tradeable() -> None:
    assert is_tradeable_session(_utc(20, 59)) is True


def test_london_newyork_overlap_is_tradeable() -> None:
    """13:00-16:00 UTC is the overlap (highest liquidity window)."""
    assert is_tradeable_session(_utc(14, 30)) is True


def test_dead_zone_not_tradeable() -> None:
    """00:00 UTC — outside both sessions."""
    assert is_tradeable_session(_utc(0, 0)) is False


def test_get_active_session_london() -> None:
    assert get_active_session(_utc(9, 0)) == "london"


def test_get_active_session_newyork() -> None:
    assert get_active_session(_utc(18, 0)) == "newyork"


def test_get_active_session_none_outside() -> None:
    assert get_active_session(_utc(3, 0)) is None


def test_get_active_session_overlap_returns_a_session() -> None:
    """During 13:00-16:00 overlap, a valid session name is returned (not None)."""
    result = get_active_session(_utc(14, 0))
    assert result in {"london", "newyork"}
