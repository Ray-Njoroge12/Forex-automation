"""Economic calendar blackout filter.

Blocks new signals 30 minutes before and 15 minutes after high-impact events
that affect currencies in the traded symbol.

The calendar JSON at data/economic_calendar.json should be updated weekly.
Schema: [{"datetime_utc": "2026-03-01T14:30:00Z", "currency": "USD", "impact": "high", "event": "..."}]
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger("fx_ai_engine.calendar_filter")

# Blackout window around a high-impact event (in minutes)
BLACKOUT_BEFORE_MINUTES = 30
BLACKOUT_AFTER_MINUTES = 15

# Currency pairs map: symbol → (base_currency, quote_currency)
_SYMBOL_CURRENCIES: dict[str, tuple[str, str]] = {
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "AUDUSD": ("AUD", "USD"),
    "USDCAD": ("USD", "CAD"),
    "USDCHF": ("USD", "CHF"),
}


class CalendarEvent(TypedDict):
    datetime_utc: str
    currency: str
    impact: str
    event: str


def load_calendar(path: str | Path) -> list[CalendarEvent]:
    """Load events from the local calendar JSON file.

    Returns an empty list if the file does not exist or fails to parse.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("Calendar file not found: %s — blackout filter disabled", p)
        return []
    try:
        with open(p) as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to load calendar %s: %s — blackout filter disabled", p, exc)
        return []


def is_news_blackout(
    symbol: str,
    now_utc: datetime,
    events: list[CalendarEvent],
) -> bool:
    """Return True if *now_utc* is within the blackout window of a high-impact event
    affecting the currencies in *symbol*.
    """
    currencies = _SYMBOL_CURRENCIES.get(symbol)
    if currencies is None:
        return False

    blackout_before = timedelta(minutes=BLACKOUT_BEFORE_MINUTES)
    blackout_after = timedelta(minutes=BLACKOUT_AFTER_MINUTES)

    for evt in events:
        if evt.get("impact", "").lower() != "high":
            continue
        if evt.get("currency", "") not in currencies:
            continue
        try:
            evt_dt = datetime.fromisoformat(evt["datetime_utc"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue

        if (evt_dt - blackout_before) <= now_utc <= (evt_dt + blackout_after):
            logger.info(
                "News blackout: symbol=%s event='%s' currency=%s at %s",
                symbol,
                evt.get("event", ""),
                evt.get("currency", ""),
                evt_dt.isoformat(),
            )
            return True

    return False
