"""Conservative quote-bar execution primitives for research replay.

Bars have explicit UTC start and end times. Only quotes strictly later than
submission can fill a new order. Protective exits may only be evaluated once
the bar has completed and are reported at bar end. This is not a complete
portfolio simulator and does not establish live-execution parity.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _positive(value: float, name: str) -> float:
    if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


@dataclass(frozen=True)
class QuoteBar:
    start: pd.Timestamp
    end: pd.Timestamp
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc(self.start))
        object.__setattr__(self, "end", _utc(self.end))
        if self.end <= self.start:
            raise ValueError("quote bar end must be after its start")
        for side in ("bid", "ask"):
            values = {name: _positive(getattr(self, f"{side}_{name}"), f"{side}_{name}")
                      for name in ("open", "high", "low", "close")}
            if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
                raise ValueError(f"invalid {side} OHLC range")
        # Corresponding extrema need not occur simultaneously; only the open
        # and close are paired observations and can be checked for crossings.
        if self.ask_open < self.bid_open or self.ask_close < self.bid_close:
            raise ValueError("ask must not be below bid at open or close")


@dataclass(frozen=True)
class ReplayFill:
    # Protective fills are reported at bar end; the exact intrabar time is unknown.
    timestamp: pd.Timestamp
    price: float
    reason: str
    ambiguous: bool = False


def market_entry(*, direction: str, submitted_at: pd.Timestamp, quote: QuoteBar,
                 pip_size: float, slippage_pips: float = 0.0) -> ReplayFill | None:
    """Fill a market order at the first supplied eligible bar open.

    The caller must supply quote bars chronologically and choose the first one
    after submission. Bid/ask already includes spread; only slippage is added.
    """
    if direction not in {"BUY", "SELL"}:
        raise ValueError("direction must be BUY or SELL")
    pip = _positive(pip_size, "pip_size")
    if isinstance(slippage_pips, bool) or not math.isfinite(float(slippage_pips)) or slippage_pips < 0:
        raise ValueError("slippage_pips must be finite and nonnegative")
    if quote.start <= _utc(submitted_at):
        return None
    price = quote.ask_open + slippage_pips * pip if direction == "BUY" else quote.bid_open - slippage_pips * pip
    return ReplayFill(quote.start, _positive(price, "fill price"), "MARKET_ENTRY")


def protective_exit(*, direction: str, active_before: pd.Timestamp, quote: QuoteBar,
                    stop_price: float, target_price: float, pip_size: float,
                    slippage_pips: float = 0.0) -> ReplayFill | None:
    """Model a stop/target active before the bar; ambiguous intrabar paths use the stop.

    Gap-through stops fill at the worse bar-open quote, with adverse slippage.
    Targets fill at their limit price without price improvement. This is a
    conservative bar-level assumption, not a broker execution guarantee. Call
    only after the bar is complete; intrabar fill time cannot be recovered.
    """
    if direction not in {"BUY", "SELL"}:
        raise ValueError("direction must be BUY or SELL")
    stop = _positive(stop_price, "stop_price")
    target = _positive(target_price, "target_price")
    pip = _positive(pip_size, "pip_size")
    if isinstance(slippage_pips, bool) or not math.isfinite(float(slippage_pips)) or slippage_pips < 0:
        raise ValueError("slippage_pips must be finite and nonnegative")
    if (direction == "BUY" and stop >= target) or (direction == "SELL" and stop <= target):
        raise ValueError("stop and target are reversed")
    if quote.start <= _utc(active_before):
        return None
    slip = slippage_pips * pip
    if direction == "BUY":
        stopped, targeted = quote.bid_low <= stop, quote.bid_high >= target
        if quote.bid_open >= target:
            return ReplayFill(quote.end, target, "TARGET")
        if stopped:
            return ReplayFill(quote.end, _positive(min(stop, quote.bid_open) - slip, "fill price"), "STOP", targeted and quote.bid_open > stop)
    else:
        stopped, targeted = quote.ask_high >= stop, quote.ask_low <= target
        if quote.ask_open <= target:
            return ReplayFill(quote.end, target, "TARGET")
        if stopped:
            return ReplayFill(quote.end, max(stop, quote.ask_open) + slip, "STOP", targeted and quote.ask_open < stop)
    if targeted:
        return ReplayFill(quote.end, target, "TARGET")
    return None
