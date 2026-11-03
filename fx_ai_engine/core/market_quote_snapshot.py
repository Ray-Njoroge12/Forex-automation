"""Read-only, demo-only EA quote feed; not an execution or strategy adapter."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class QuoteSnapshotError(ValueError):
    """A quote snapshot cannot safely be used."""


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    bid: float
    ask: float
    age_seconds: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid


def load_demo_quotes(path: Path, *, now: datetime | None = None, max_snapshot_age: float = 15.0,
                     max_quote_age: float = 30.0) -> dict[str, MarketQuote]:
    """Validate a fresh MT5 demo quote snapshot; return only fresh valid quotes.

    Missing or stale individual symbols are omitted. An invalid envelope or a
    malformed quote fails the entire snapshot. No order-routing capability is
    provided by this function.
    """
    if any(not math.isfinite(value) or value <= 0 for value in (max_snapshot_age, max_quote_age)):
        raise ValueError("age limits must be finite and positive")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise QuoteSnapshotError("unsupported quote snapshot schema")
        if any(data.get(key) is not True for key in ("account_is_demo", "account_authenticated", "terminal_connected")):
            raise QuoteSnapshotError("connected authenticated demo account required")
        stamp = datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise QuoteSnapshotError("snapshot timestamp must have a timezone")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise QuoteSnapshotError("current time must have a timezone")
        age = (current - stamp).total_seconds()
        if age < -5 or age > max_snapshot_age:
            raise QuoteSnapshotError("snapshot is stale or from the future")
        quotes = data["quotes"]
        if not isinstance(quotes, list):
            raise QuoteSnapshotError("quotes must be a list")
        result: dict[str, MarketQuote] = {}
        seen: set[str] = set()
        for item in quotes:
            if not isinstance(item, dict):
                raise QuoteSnapshotError("invalid quote")
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isascii() or not symbol.isupper() or not symbol.isalpha() or symbol in seen:
                raise QuoteSnapshotError("invalid or duplicate quote symbol")
            seen.add(symbol)
            bid, ask, tick_age = (item.get(key) for key in ("bid", "ask", "tick_age_seconds"))
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (bid, ask, tick_age)):
                raise QuoteSnapshotError("quote values must be finite numbers")
            if bid <= 0 or ask < bid or tick_age < 0:
                raise QuoteSnapshotError("invalid quote prices or age")
            total_age = float(tick_age) + max(0.0, age)
            if total_age <= max_quote_age:
                result[symbol] = MarketQuote(symbol, float(bid), float(ask), total_age)
        return result
    except QuoteSnapshotError:
        raise
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        raise QuoteSnapshotError(f"quote snapshot unavailable or invalid: {exc}") from exc
