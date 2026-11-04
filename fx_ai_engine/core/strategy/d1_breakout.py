"""H-009 ``d1_breakout_v1``: 20-day breakout trend following. REPLAY/RESEARCH ONLY.

Direction from D1 and a D1-ATR stop change the SRS v1 timeframe model (5.2) and
stop basis (5.3). Owner decision 2026-11-04: registered ``live_eligible=False``
until an SRS v1.x revision is approved; ``build_strategy(run_context="live")``
and ``main.Engine`` refuse it (tests/test_strategy_live_eligibility.py).

Rules (frozen in ``docs/research/hypotheses/H-009-d1-breakout.md``):
  * Direction: the last closed D1 bar closes above the highest high (BUY) or
    below the lowest low (SELL) of the 20 D1 bars before it.
  * Entry: the first session decision (``is_tradeable_session``) at or after
    that D1 bar's close, found from the M15 bar closes between the D1 close and
    now (no stored state).
  * Regime: reject if the H1 regime trends against the direction.
  * Stop: 2.0 x ATR(20) on D1; 40-300 pips.
  * Fixed spread-adjusted 2.2R target; no break-even, partial close or trail.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core.filters.session_filter import is_tradeable_session
from core.indicators import calculate_atr
from core.strategy.base import M15, TIMEFRAME_D1, TIMEFRAME_M15, StrategyDecision
from core.strategy.common import StrategyBase, against_trend, decision_time
from core.strategy.registry import param_range, register_strategy

STRATEGY_ID = "d1_breakout_v1"
LIVE_BLOCK_REASON = ("requires SRS v1.x revision of timeframe model (§5.2) and stop basis (§5.3); "
                     "owner decision 2026-11-04: replay/research only")
M15_FETCH_BARS = 350
D1 = pd.Timedelta(days=1)


@dataclass(frozen=True)
class Params:
    channel_d1_bars: int = field(default=20, metadata=param_range(5, 100))
    atr_d1_period: int = field(default=20, metadata=param_range(5, 50))
    stop_atr_mult: float = field(default=2.0, metadata=param_range(0.5, 5.0))
    min_stop_pips: float = field(default=40.0, metadata=param_range(10, 500))
    max_stop_pips: float = field(default=300.0, metadata=param_range(20, 1000))

    def __post_init__(self) -> None:
        if not self.min_stop_pips < self.max_stop_pips:
            raise ValueError("d1_breakout_v1 params need min_stop_pips < max_stop_pips")

    @property
    def d1_fetch_bars(self) -> int:
        return max(self.channel_d1_bars, self.atr_d1_period) + 1


class D1BreakoutStrategy(StrategyBase):
    strategy_id = STRATEGY_ID
    prefix = "D1T"

    def evaluate(self) -> StrategyDecision:
        rejected = self._start()
        if rejected is not None:
            return rejected
        p: Params = self.params
        m15 = self.fetch_ohlc(self.symbol, TIMEFRAME_M15, M15_FETCH_BARS)
        d1 = self.fetch_ohlc(self.symbol, TIMEFRAME_D1, p.d1_fetch_bars)
        if m15.empty or d1.empty:
            return self._reject("DATA_UNAVAILABLE", f"m15={len(m15)} d1={len(d1)}")
        d1 = d1.tail(p.d1_fetch_bars)  # a research feed may serve a longer window (lookback override)
        if len(d1) < p.d1_fetch_bars:
            return self._reject("WARMUP_INCOMPLETE", f"d1={len(d1)} required>={p.d1_fetch_bars}")
        now = decision_time(m15)
        if not is_tradeable_session(now.to_pydatetime()):
            return self._reject("OUTSIDE_SESSION", f"decision={now.isoformat()}")

        last = d1.iloc[-1]
        prior = d1.iloc[-(p.channel_d1_bars + 1):-1]
        if pd.isna(last["close"]) or prior[["high", "low"]].isna().any().any():
            return self._reject("INDICATORS_UNAVAILABLE", "d1 ohlc=nan")
        close = float(last["close"])
        high, low = float(prior["high"].max()), float(prior["low"].min())
        base = f"d1_close={close:.5f} channel_high={high:.5f} channel_low={low:.5f}"
        if close > high:
            direction = "BUY"
        elif close < low:
            direction = "SELL"
        else:
            return self._reject("NO_BREAKOUT", base)
        base = f"direction={direction} {base}"

        d1_close_time = pd.Timestamp(d1.index[-1]).tz_convert("UTC") + D1
        bar_closes = m15.index + M15
        if bar_closes[0] > d1_close_time:
            return self._reject("WARMUP_INCOMPLETE", f"{base} m15 history starts after the D1 close")
        between = bar_closes[(bar_closes >= d1_close_time) & (bar_closes < now)]
        if any(is_tradeable_session(t.to_pydatetime()) for t in between):
            return self._reject("NOT_FIRST_SESSION_DECISION", f"{base} d1_close_time={d1_close_time.isoformat()}")
        if against_trend(self._regime_out, direction):
            return self._reject("AGAINST_H1_TREND", f"{base} regime={self._regime_out.regime}")

        atr = calculate_atr(d1, p.atr_d1_period).iloc[-1]
        if pd.isna(atr) or float(atr) <= 0:
            return self._reject("INDICATORS_UNAVAILABLE", f"{base} atr_d1={atr}")
        stop_pips = p.stop_atr_mult * float(atr) / self.pip
        base = f"{base} atr_d1_pips={float(atr) / self.pip:.2f}"
        out_of_range = self._stop_in_range(stop_pips, base)
        if out_of_range is not None:
            return out_of_range
        return self._signal(direction=direction, stop_pips=stop_pips, m15=m15,
                            reason_code=f"D1T_BREAKOUT_{direction}", details=base)


def _factory(*, symbol, fetch_ohlc, fetch_spread, policy, params, regime_cache_key):
    return D1BreakoutStrategy(symbol, fetch_ohlc, fetch_spread, policy, params,
                              regime_cache_key=regime_cache_key)


register_strategy(STRATEGY_ID, _factory, Params, live_eligible=False, live_block_reason=LIVE_BLOCK_REASON)
