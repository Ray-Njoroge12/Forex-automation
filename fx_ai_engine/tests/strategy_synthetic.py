"""Small hand-built bar frames and a stub regime for the hypothesis strategy tests."""
from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

from backtesting.unified_replay import Frames, normalise_frame
from config_microcapital import get_policy_config
from core.strategy import build_strategy
from core.types import RegimeOutput

POLICY = get_policy_config(mode_id="core_srs", env={})
WICK = 0.0002


def regime(name: str = "NO_TRADE") -> RegimeOutput:
    trend = {"TRENDING_BULL": "BULLISH", "TRENDING_BEAR": "BEARISH", "TRANSITION": "TRANSITION"}.get(name, "FLAT")
    reason = {"TRENDING_BULL": "REGIME_TREND_BULL", "TRENDING_BEAR": "REGIME_TREND_BEAR",
              "TRANSITION": "REGIME_TRANSITION"}.get(name, "REGIME_NO_TRADE")
    return RegimeOutput(name, trend, "NORMAL", 0.5, reason, "t", 1.0)


class StubRegime:
    """Stands in for ``CachedRegime`` so rule branches can be tested under a chosen H1 regime."""

    def __init__(self, output: RegimeOutput):
        self.output = output

    def evaluate(self) -> tuple[RegimeOutput, str]:
        return self.output, f"stub regime={self.output.regime}"


def bars(times: Sequence[pd.Timestamp] | pd.DatetimeIndex, closes: Sequence[float], *, wick: float = WICK,
         first_open: float | None = None) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0] if first_open is None else first_open], closes[:-1]])
    return normalise_frame(pd.DataFrame({
        "open": opens, "high": np.maximum(opens, closes) + wick, "low": np.minimum(opens, closes) - wick,
        "close": closes, "volume": 1.0, "spread_points": 0.0}, index=pd.DatetimeIndex(times)))


def m15(start: str, closes: Sequence[float], **kw) -> pd.DataFrame:
    return bars(pd.date_range(start, periods=len(closes), freq="15min", tz="UTC"), closes, **kw)


def resample(frame: pd.DataFrame, rule: str, offset: str | None = None) -> pd.DataFrame:
    agg = frame.resample(rule, offset=offset, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "spread_points": "max"})
    return agg.dropna(subset=["close"])


def frames_for(symbol: str, m15_frame: pd.DataFrame, *, d1: pd.DataFrame | None = None,
               h1: pd.DataFrame | None = None) -> Frames:
    return {symbol: {"M15": m15_frame, "H1": resample(m15_frame, "1h") if h1 is None else h1,
                     "H4": resample(m15_frame, "4h"),
                     "D1": resample(m15_frame, "1D", offset="22h") if d1 is None else d1}}


def factory(strategy_id: str, symbol: str, stub: RegimeOutput | None, *, params=None, spread: bool = True):
    """``assert_strategy_contract`` factory; ``stub`` replaces the H1 regime, ``spread=False`` serves None."""
    def build(fetch, fetch_spread):
        strategy = build_strategy(strategy_id, symbol=symbol, fetch_ohlc=fetch,
                                  fetch_spread=fetch_spread if spread else (lambda s: None),
                                  policy=POLICY, run_context="research", params=params)
        if stub is not None:
            strategy.inner.regime = StubRegime(stub)
        return strategy
    return build
