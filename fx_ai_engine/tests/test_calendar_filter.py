from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.filters.calendar_filter import is_news_blackout


def _event(offset_minutes: int, currency: str = "USD") -> dict:
    """Build a CalendarEvent dict relative to now."""
    dt = datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)
    return {"datetime_utc": dt.isoformat(), "currency": currency, "impact": "high", "event": "Test"}


def test_blackout_fires_20min_before_event() -> None:
    """Signal 20 min before event is within 30-min pre-window."""
    events = [_event(offset_minutes=20)]
    now = datetime.now(timezone.utc)
    assert is_news_blackout("EURUSD", now, events) is True


def test_no_blackout_60min_before_event() -> None:
    """Signal 60 min before event is outside pre-window."""
    events = [_event(offset_minutes=60)]
    now = datetime.now(timezone.utc)
    assert is_news_blackout("EURUSD", now, events) is False


def test_blackout_fires_10min_after_event() -> None:
    """Signal 10 min after event is within 15-min post-window."""
    events = [_event(offset_minutes=-10)]
    now = datetime.now(timezone.utc)
    assert is_news_blackout("EURUSD", now, events) is True


def test_no_blackout_20min_after_event() -> None:
    """Signal 20 min after event is outside post-window (window is +15 min)."""
    events = [_event(offset_minutes=-20)]
    now = datetime.now(timezone.utc)
    assert is_news_blackout("EURUSD", now, events) is False


def test_medium_impact_does_not_block() -> None:
    """Only high-impact events trigger blackout."""
    evt = _event(offset_minutes=10)
    evt["impact"] = "medium"
    assert is_news_blackout("EURUSD", datetime.now(timezone.utc), [evt]) is False


def test_currency_mismatch_does_not_block() -> None:
    """USD event does not block AUDJPY (neither currency is USD)."""
    events = [_event(offset_minutes=10, currency="USD")]
    assert is_news_blackout("AUDJPY", datetime.now(timezone.utc), events) is False


def test_base_currency_match_blocks() -> None:
    """EUR event blocks EURUSD trade (EUR is base currency)."""
    events = [_event(offset_minutes=15, currency="EUR")]
    assert is_news_blackout("EURUSD", datetime.now(timezone.utc), events) is True


def test_quote_currency_match_blocks() -> None:
    """USD event blocks GBPUSD trade (USD is quote currency)."""
    events = [_event(offset_minutes=15, currency="USD")]
    assert is_news_blackout("GBPUSD", datetime.now(timezone.utc), events) is True


def test_jpy_event_blocks_usdjpy() -> None:
    """JPY event blocks USDJPY trade."""
    events = [_event(offset_minutes=10, currency="JPY")]
    assert is_news_blackout("USDJPY", datetime.now(timezone.utc), events) is True


def test_empty_calendar_never_blocks() -> None:
    """Empty event list never triggers blackout."""
    assert is_news_blackout("EURUSD", datetime.now(timezone.utc), []) is False


def test_missing_impact_field_does_not_block() -> None:
    """Event without 'impact' field is ignored safely."""
    evt = {"datetime_utc": datetime.now(timezone.utc).isoformat(), "currency": "USD", "event": "Test"}
    assert is_news_blackout("EURUSD", datetime.now(timezone.utc), [evt]) is False
