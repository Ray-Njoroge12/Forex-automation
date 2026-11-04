"""H-006 ``london_breakout_v1``: London-open breakout of the Asian range (within SRS v1).

Rules (frozen in ``docs/research/hypotheses/H-006-london-breakout.md``):
  * Asian range: high/low of the M15 bars opening in [00:00, 07:00) Europe/London
    on the decision's London date. Every bar must be present (28 on a normal day;
    the count follows the real elapsed time on the two DST-change days), else
    ``LBO_RANGE_INCOMPLETE``.
  * Entry window: decision time in (07:00, 10:00] London.
  * Trigger: last M15 close above the range high (BUY) or below the range low
    (SELL), and no earlier M15 close since 07:00 London that day outside the
    range on either side (one attempt per symbol per day, no stored state).
  * Regime: reject if the H1 regime trends against the breakout.
  * Stop: opposite side of the range +/- 1 pip, from the last close; 20-60 pips.
  * Fixed spread-adjusted 2.2R target; no break-even, partial close or trail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from core.strategy.base import M15, TIMEFRAME_M15, StrategyDecision
from core.strategy.common import StrategyBase, against_trend, decision_time
from core.strategy.registry import param_range, register_strategy

STRATEGY_ID = "london_breakout_v1"
LONDON = ZoneInfo("Europe/London")
M15_FETCH_BARS = 350


@dataclass(frozen=True)
class Params:
    range_start_hour: int = field(default=0, metadata=param_range(0, 6))
    range_end_hour: int = field(default=7, metadata=param_range(1, 8))
    entry_end_hour: int = field(default=10, metadata=param_range(8, 12))
    min_stop_pips: float = field(default=20.0, metadata=param_range(5, 100))
    max_stop_pips: float = field(default=60.0, metadata=param_range(10, 200))
    stop_buffer_pips: float = field(default=1.0, metadata=param_range(0, 5))

    def __post_init__(self) -> None:
        if not self.range_start_hour < self.range_end_hour < self.entry_end_hour:
            raise ValueError("london_breakout_v1 params need range_start_hour < range_end_hour < entry_end_hour")
        if not self.min_stop_pips < self.max_stop_pips:
            raise ValueError("london_breakout_v1 params need min_stop_pips < max_stop_pips")


def _london(day, hour: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day, time(hour)), tz=LONDON).tz_convert("UTC")


class LondonBreakoutStrategy(StrategyBase):
    strategy_id = STRATEGY_ID
    prefix = "LBO"

    def evaluate(self) -> StrategyDecision:
        rejected = self._start()
        if rejected is not None:
            return rejected
        p: Params = self.params
        m15 = self.fetch_ohlc(self.symbol, TIMEFRAME_M15, M15_FETCH_BARS)
        if m15.empty:
            return self._reject("DATA_UNAVAILABLE", "m15=empty")
        if m15[["open", "high", "low", "close"]].tail(2).isna().any().any():
            return self._reject("INDICATORS_UNAVAILABLE", "m15 ohlc=nan")
        now = decision_time(m15)
        day = now.tz_convert(LONDON).date()
        range_start, range_end = _london(day, p.range_start_hour), _london(day, p.range_end_hour)
        entry_end = _london(day, p.entry_end_hour)
        if not range_end < now <= entry_end:
            return self._reject("OUTSIDE_ENTRY_WINDOW", f"decision={now.isoformat()} london={now.tz_convert(LONDON).time()}")

        opens = m15.index
        asian = m15[(opens >= range_start) & (opens < range_end)]
        expected = int((range_end - range_start) / M15)
        if len(asian) != expected or asian[["high", "low"]].isna().any().any():
            return self._reject("RANGE_INCOMPLETE", f"bars={len(asian)} expected={expected} date={day}")
        high, low = float(asian["high"].max()), float(asian["low"].min())

        since_open = m15[(opens >= range_end) & (opens < now)]
        last_close = float(since_open["close"].iloc[-1])
        earlier = since_open["close"].iloc[:-1]
        base = f"range_high={high:.5f} range_low={low:.5f} close={last_close:.5f}"
        if bool(((earlier > high) | (earlier < low)).any()):
            return self._reject("ALREADY_BROKEN", base)
        if last_close > high:
            direction = "BUY"
            stop_pips = (last_close - (low - p.stop_buffer_pips * self.pip)) / self.pip
        elif last_close < low:
            direction = "SELL"
            stop_pips = ((high + p.stop_buffer_pips * self.pip) - last_close) / self.pip
        else:
            return self._reject("NO_BREAKOUT", base)
        base = f"direction={direction} {base}"
        if against_trend(self._regime_out, direction):
            return self._reject("AGAINST_H1_TREND", f"{base} regime={self._regime_out.regime}")
        out_of_range = self._stop_in_range(stop_pips, base)
        if out_of_range is not None:
            return out_of_range
        return self._signal(direction=direction, stop_pips=stop_pips, m15=m15,
                            reason_code=f"LBO_BREAKOUT_{direction}", details=base)


def _factory(*, symbol, fetch_ohlc, fetch_spread, policy, params, regime_cache_key):
    return LondonBreakoutStrategy(symbol, fetch_ohlc, fetch_spread, policy, params,
                                  regime_cache_key=regime_cache_key)


register_strategy(STRATEGY_ID, _factory, Params, live_eligible=True)
