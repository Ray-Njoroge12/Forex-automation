"""H-007 ``h1_channel_breakout_v1``: two-day H1 channel breakout in the trend regime (within SRS v1).

Rules (frozen in ``docs/research/hypotheses/H-007-h1-channel-breakout.md``):
  * Regime: TRENDING_BULL allows BUY only, TRENDING_BEAR SELL only (the
    baseline's own RegimeAgent gate); any other regime is a REGIME reject.
  * Channel: highest high / lowest low of the last 48 closed H1 bars.
  * Trigger: last M15 close beyond the channel in the trend direction with the
    previous M15 close inside it (first close through).
  * Stop: lowest low (BUY) / highest high (SELL) of the last 16 closed M15 bars
    -/+ 1 pip, from the last close; 20-60 pips.
  * Fixed spread-adjusted 2.2R target; no break-even, partial close or trail.

Because the channel uses closed H1 bars only, an M15 bar that closes on the
hour lies inside the last channel bar and cannot break it; triggers occur at
:15, :30 and :45.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.strategy.base import TIMEFRAME_H1, TIMEFRAME_M15, StrategyDecision
from core.strategy.common import StrategyBase, is_trending
from core.strategy.registry import param_range, register_strategy

STRATEGY_ID = "h1_channel_breakout_v1"
M15_FETCH_BARS = 350


@dataclass(frozen=True)
class Params:
    channel_h1_bars: int = field(default=48, metadata=param_range(12, 240))
    stop_lookback_m15: int = field(default=16, metadata=param_range(4, 96))
    min_stop_pips: float = field(default=20.0, metadata=param_range(5, 100))
    max_stop_pips: float = field(default=60.0, metadata=param_range(10, 200))
    stop_buffer_pips: float = field(default=1.0, metadata=param_range(0, 5))

    def __post_init__(self) -> None:
        if not self.min_stop_pips < self.max_stop_pips:
            raise ValueError("h1_channel_breakout_v1 params need min_stop_pips < max_stop_pips")


class H1ChannelBreakoutStrategy(StrategyBase):
    strategy_id = STRATEGY_ID
    prefix = "HCB"

    def evaluate(self) -> StrategyDecision:
        rejected = self._start()
        if rejected is not None:
            return rejected
        regime = self._regime_out
        if not is_trending(regime):
            return self._regime_reject(regime, "HCB_REGIME_NOT_TRENDING", self._regime_details)
        p: Params = self.params
        m15 = self.fetch_ohlc(self.symbol, TIMEFRAME_M15, M15_FETCH_BARS)
        h1 = self.fetch_ohlc(self.symbol, TIMEFRAME_H1, p.channel_h1_bars)
        if m15.empty or h1.empty:
            return self._reject("DATA_UNAVAILABLE", f"m15={len(m15)} h1={len(h1)}")
        if len(m15) < max(p.stop_lookback_m15, 2) or len(h1) < p.channel_h1_bars:
            return self._reject("WARMUP_INCOMPLETE", f"m15={len(m15)} h1={len(h1)} "
                                f"required m15>={max(p.stop_lookback_m15, 2)} h1>={p.channel_h1_bars}")
        h1 = h1.tail(p.channel_h1_bars)  # a research feed may serve a longer window (lookback override)
        swing = m15.tail(p.stop_lookback_m15)
        values = [h1["high"], h1["low"], m15["close"].tail(2), swing["high"], swing["low"]]
        if any(v.isna().any() for v in values):
            return self._reject("INDICATORS_UNAVAILABLE", "ohlc=nan")
        high, low = float(h1["high"].max()), float(h1["low"].min())
        last_close, prev_close = float(m15["close"].iloc[-1]), float(m15["close"].iloc[-2])
        direction = "BUY" if regime.regime == "TRENDING_BULL" else "SELL"
        base = (f"direction={direction} channel_high={high:.5f} channel_low={low:.5f} "
                f"close={last_close:.5f} prev_close={prev_close:.5f}")
        prev_inside = low <= prev_close <= high
        if direction == "BUY" and last_close > high and prev_inside:
            stop_pips = (last_close - (float(swing["low"].min()) - p.stop_buffer_pips * self.pip)) / self.pip
        elif direction == "SELL" and last_close < low and prev_inside:
            stop_pips = ((float(swing["high"].max()) + p.stop_buffer_pips * self.pip) - last_close) / self.pip
        else:
            return self._reject("NO_BREAKOUT", base)
        out_of_range = self._stop_in_range(stop_pips, base)
        if out_of_range is not None:
            return out_of_range
        return self._signal(direction=direction, stop_pips=stop_pips, m15=m15,
                            reason_code=f"HCB_BREAKOUT_{direction}", details=base)


def _factory(*, symbol, fetch_ohlc, fetch_spread, policy, params, regime_cache_key):
    return H1ChannelBreakoutStrategy(symbol, fetch_ohlc, fetch_spread, policy, params,
                                     regime_cache_key=regime_cache_key)


register_strategy(STRATEGY_ID, _factory, Params, live_eligible=True)
