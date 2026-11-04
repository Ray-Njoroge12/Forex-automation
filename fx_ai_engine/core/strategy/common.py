"""Helpers shared by the strategies (stage D design section 4).

* Regime: the H1 ``RegimeAgent`` with the active policy, optionally cached per
  ``regime_cache_key`` (replay passes the closed-H1 count; live passes None).
* Clock: decision time = open of the last closed M15 bar + 15 minutes.
* Target: the spread-adjusted formula of ``TechnicalAgent`` (min R:R from the policy).
* Exits: fixed bracket (no break-even, partial close or trail).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping

import pandas as pd

from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalThresholds
from core.indicators import calculate_rsi
from core.strategy.base import (
    M15, TIMEFRAME_H1, TRENDING_REGIMES, FetchSpread, StrategyDecision, new_trade_id, regime_rejected,
    utc_now_iso,
)
from core.types import RegimeOutput, TechnicalSignal

REGIME_DATA_REJECTS = frozenset({"REGIME_DATA_UNAVAILABLE", "REGIME_WARMUP_INCOMPLETE"})
SIGNAL_CONFIDENCE = 0.5  # not used for gating or sizing anywhere


def pip_size(symbol: str) -> float:
    return 0.01 if "JPY" in symbol else 0.0001


class CachedRegime:
    """H1 regime; reuses the last result while ``cache_key()`` is unchanged."""

    def __init__(self, symbol: str, fetch_ohlc, policy: Mapping[str, object],
                 cache_key: Callable[[], Any] | None = None):
        self.agent = RegimeAgent(symbol, fetch_ohlc, policy=policy)
        self.cache_key = cache_key
        self._key: Any = object()
        self._cached: tuple[RegimeOutput, str] | None = None

    def evaluate(self) -> tuple[RegimeOutput, str]:
        if self.cache_key is None:
            regime = self.agent.evaluate(TIMEFRAME_H1)
            return regime, self.agent.last_details
        key = self.cache_key()
        if self._cached is None or key != self._key:
            regime = self.agent.evaluate(TIMEFRAME_H1)
            self._cached = (regime, self.agent.last_details)
            self._key = key
        return self._cached


def decision_time(m15: pd.DataFrame) -> pd.Timestamp:
    """Close of the last completed M15 bar (identical live and in replay)."""
    return pd.Timestamp(m15.index[-1]).tz_convert("UTC") + M15


def against_trend(regime: RegimeOutput, direction: str) -> bool:
    return (regime.regime == "TRENDING_BULL" and direction == "SELL") or (
        regime.regime == "TRENDING_BEAR" and direction == "BUY")


def is_trending(regime: RegimeOutput) -> bool:
    return regime.regime in TRENDING_REGIMES


def spread_adjusted_target(stop_pips: float, spread_pips: float, min_rr: float) -> tuple[float, float]:
    """``(take_profit_pips, effective_rr)`` exactly as ``TechnicalAgent`` computes them."""
    effective_stop = stop_pips + spread_pips / 2
    min_tp_for_rr = min_rr * effective_stop + spread_pips / 2
    take_profit = max(float(stop_pips * min_rr), min_tp_for_rr)
    effective_tp = take_profit - spread_pips / 2
    rr = effective_tp / effective_stop if effective_stop > 0 else 0.0
    return take_profit, rr


class StrategyBase:
    """Common state and decision builders for the hypothesis strategies."""

    strategy_id = ""
    prefix = ""

    def __init__(self, symbol: str, fetch_ohlc, fetch_spread: FetchSpread | None, policy: Mapping[str, object],
                 params: Any, *, regime_cache_key: Callable[[], Any] | None = None):
        self.symbol = symbol
        self.fetch_ohlc = fetch_ohlc
        self.fetch_spread = fetch_spread
        self.policy = policy
        self.params = params
        self.min_rr = float(policy["MIN_RISK_REWARD"])
        self.rsi_period = TechnicalThresholds.from_policy(policy).rsi_period
        self.pip = pip_size(symbol)
        self.regime = CachedRegime(symbol, fetch_ohlc, policy, regime_cache_key)
        self._regime_out: RegimeOutput | None = None
        self._regime_details = ""

    # -- decisions ---------------------------------------------------------
    def _regime_reject(self, regime: RegimeOutput, reason_code: str, details: str) -> StrategyDecision:
        return regime_rejected(self.strategy_id, regime, reason_code, details)

    def _reject(self, reason: str, details: str = "") -> StrategyDecision:
        assert self._regime_out is not None
        return StrategyDecision(self.strategy_id, self._regime_out, True, self._regime_out.reason_code,
                                self._regime_details, None, f"{self.prefix}_{reason}", details)

    def _start(self) -> StrategyDecision | None:
        """Evaluate the regime; return a REGIME reject for missing or warming-up H1 data."""
        regime, details = self.regime.evaluate()
        self._regime_out, self._regime_details = regime, details
        if regime.reason_code in REGIME_DATA_REJECTS:
            return self._regime_reject(regime, regime.reason_code, details)
        return None

    def _signal(self, *, direction: str, stop_pips: float, m15: pd.DataFrame, reason_code: str,
                details: str) -> StrategyDecision:
        """Fixed-bracket MARKET signal, or a spread/indicator reject."""
        spread = self.fetch_spread(self.symbol) if self.fetch_spread else None
        if spread is None or not math.isfinite(float(spread)) or float(spread) < 0:
            return self._reject("SPREAD_UNAVAILABLE", f"spread={spread}")
        spread_pips = float(spread) / self.pip
        rsi = calculate_rsi(m15["close"], self.rsi_period)
        if pd.isna(rsi.iloc[-1]):
            return self._reject("INDICATORS_UNAVAILABLE", "rsi=nan")
        rsi_slope = 0.0
        if len(rsi) >= 4 and not pd.isna(rsi.iloc[-3]):
            rsi_slope = round(float(rsi.iloc[-1]) - float(rsi.iloc[-3]), 2)
        take_profit, rr = spread_adjusted_target(stop_pips, spread_pips, self.min_rr)
        if rr < self.min_rr:
            return self._reject("RR_BELOW_MIN", f"rr={rr:.2f} min_rr={self.min_rr:.2f}")
        assert self._regime_out is not None
        signal = TechnicalSignal(
            trade_id=new_trade_id(),
            symbol=self.symbol,
            direction=direction,
            stop_pips=round(stop_pips, 2),
            take_profit_pips=round(take_profit, 2),
            risk_reward=round(rr, 2),
            confidence=SIGNAL_CONFIDENCE,
            reason_code=reason_code,
            timestamp_utc=utc_now_iso(),
            rsi_at_entry=round(float(rsi.iloc[-1]), 2),
            spread_entry=round(spread_pips, 2),
            rsi_slope=rsi_slope,
            order_type="MARKET",
            be_trigger_r=0.0,
            partial_close_r=0.0,
            trailing_atr_mult=0.0,
            tp_mode="FIXED",
            structural_sl_pips=round(stop_pips, 2),
        )
        full = (f"{details} rr={rr:.2f} spread_pips={spread_pips:.2f} stop_pips={stop_pips:.2f} "
                f"tp_pips={take_profit:.2f}")
        return StrategyDecision(self.strategy_id, self._regime_out, True, self._regime_out.reason_code,
                                self._regime_details, signal, reason_code, full)

    def _stop_in_range(self, stop_pips: float, details: str) -> StrategyDecision | None:
        if not math.isfinite(stop_pips) or stop_pips < self.params.min_stop_pips:
            return self._reject("STOP_TOO_TIGHT", f"{details} stop_pips={stop_pips:.2f} min={self.params.min_stop_pips:g}")
        if stop_pips > self.params.max_stop_pips:
            return self._reject("STOP_TOO_WIDE", f"{details} stop_pips={stop_pips:.2f} max={self.params.max_stop_pips:g}")
        return None
