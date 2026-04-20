"""Adapter scaffolding for integrating existing core agents into the
meta-labeling replay pipeline.

This module is intentionally additive and does not modify live trading flow.
It provides:
1) Historical OHLC provider that mimics core fetch_ohlc callbacks.
2) Regime and technical adapters that satisfy replay Protocol contracts.
3) Label/schema normalization helpers for known integration mismatches.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

import pandas as pd

from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.indicators import calculate_atr
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15
from core.types import RegimeOutput as CoreRegimeOutput
from core.types import TechnicalSignal as CoreTechnicalSignal
from ml.meta_labeler.signal_replay import RegimeOutput, TechnicalSignal

TIMEFRAME_H4 = 16388

_ENGINE_TO_REPLAY_REGIME: dict[str, str] = {
    "TRENDING_BULL": "TREND_UP",
    "TRENDING_BEAR": "TREND_DOWN",
    "TRANSITION": "TRANSITION",
    "RANGING_LOW_VOL": "RANGE",
    "RANGING_HIGH_VOL": "HIGH_VOL",
    "NO_TRADE": "NO_REGIME",
}

_REPLAY_TO_ENGINE_REGIME: dict[str, str] = {
    "TREND_UP": "TRENDING_BULL",
    "TREND_DOWN": "TRENDING_BEAR",
    "TREND": "TRENDING_BULL",
    "TRENDING": "TRENDING_BULL",
    "RANGE": "RANGING_LOW_VOL",
    "RANGING": "RANGING_LOW_VOL",
    "TRANSITION": "TRANSITION",
    "HIGH_VOL": "RANGING_HIGH_VOL",
    "NO_REGIME": "NO_TRADE",
}

_FEATURE_REGIME_LABEL_MAP: dict[str, str] = {
    "TRENDING_BULL": "TREND_UP",
    "TRENDING_BEAR": "TREND_DOWN",
    "RANGING_LOW_VOL": "RANGE",
    "RANGING_HIGH_VOL": "HIGH_VOL",
    "NO_TRADE": "UNKNOWN",
}


def normalize_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize bridge-shaped OHLCV into feature-builder expected columns.

    Current bridge snapshots expose ``tick_volume``. FeatureBuilder expects
    ``volume`` for microstructure features.
    """
    out = df.copy()
    if "tick_volume" in out.columns and "volume" not in out.columns:
        out = out.rename(columns={"tick_volume": "volume"})
    return out


def normalize_regime_label_for_features(regime_label: str | None) -> str:
    """Normalize engine regime labels to feature schema labels.

    This avoids silently mapping known engine labels to UNKNOWN(0).
    """
    key = str(regime_label or "").strip().upper()
    if not key:
        return "UNKNOWN"
    return _FEATURE_REGIME_LABEL_MAP.get(key, key)


def map_engine_regime_to_replay(regime_label: str) -> str:
    """Map existing engine regime labels to replay-compatible labels."""
    return _ENGINE_TO_REPLAY_REGIME.get(str(regime_label or "").upper(), "NO_REGIME")


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _pip_size(symbol: str) -> float:
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def _derive_h4_from_h1(h1_df: pd.DataFrame) -> pd.DataFrame:
    if h1_df.empty:
        return pd.DataFrame()
    required = {"open", "high", "low", "close"}
    missing = required - set(h1_df.columns)
    if missing:
        raise ValueError(f"H1 input missing required columns for H4 derivation: {missing}")

    agg: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in h1_df.columns:
        agg["volume"] = "sum"
    if "spread" in h1_df.columns:
        agg["spread"] = "mean"

    h4_df = h1_df.resample("4h", label="right", closed="right").agg(agg)
    h4_df = h4_df.dropna(subset=["open", "high", "low", "close"])
    if "spread" in h4_df.columns:
        h4_df["spread"] = h4_df["spread"].round().astype("int32")
    return h4_df


@dataclass
class HistoricalOhlcProvider:
    """Provides point-in-time OHLC slices to core agents during replay."""

    symbol: str
    m15_df: pd.DataFrame
    h1_df: pd.DataFrame
    h4_df: pd.DataFrame | None = None
    cutoff: pd.Timestamp | None = None

    def __post_init__(self) -> None:
        self.symbol = self.symbol.upper()
        self.m15_df = normalize_ohlcv_columns(self.m15_df)
        self.h1_df = normalize_ohlcv_columns(self.h1_df)
        if self.h4_df is None:
            self.h4_df = _derive_h4_from_h1(self.h1_df)
        else:
            self.h4_df = normalize_ohlcv_columns(self.h4_df)

    def set_cutoff(self, cutoff: pd.Timestamp) -> None:
        self.cutoff = cutoff

    def _source_for_timeframe(self, timeframe: int) -> pd.DataFrame:
        if timeframe == TIMEFRAME_M15:
            return self.m15_df
        if timeframe == TIMEFRAME_H1:
            return self.h1_df
        if timeframe == TIMEFRAME_H4:
            return self.h4_df if self.h4_df is not None else pd.DataFrame()
        return pd.DataFrame()

    def fetch(self, symbol: str, timeframe: int, num_candles: int = 350) -> pd.DataFrame:
        """Signature-compatible fetch callback for core agents."""
        if symbol.upper() != self.symbol:
            return pd.DataFrame()

        source = self._source_for_timeframe(timeframe)
        if source.empty:
            return pd.DataFrame()

        scoped = source if self.cutoff is None else source.loc[: self.cutoff]
        if num_candles <= 0:
            return scoped.copy()
        return scoped.tail(num_candles).copy()


class CoreRegimeAgentAdapter:
    """Adapts the existing core RegimeAgent to replay `classify(h1_df)` protocol."""

    def __init__(
        self,
        regime_agent: RegimeAgent,
        history_provider: HistoricalOhlcProvider,
        *,
        regime_map: Mapping[str, str] | None = None,
    ) -> None:
        self._regime_agent = regime_agent
        self._history_provider = history_provider
        self._regime_map = dict(regime_map or _ENGINE_TO_REPLAY_REGIME)

    def classify(self, h1_df: pd.DataFrame) -> RegimeOutput:
        if h1_df.empty:
            return RegimeOutput(regime="NO_REGIME", confidence=0.0)

        self._history_provider.set_cutoff(h1_df.index[-1])
        engine_out = self._regime_agent.evaluate(TIMEFRAME_H1)
        replay_regime = self._regime_map.get(engine_out.regime, "NO_REGIME")
        return RegimeOutput(
            regime=replay_regime,
            confidence=_clamp01(engine_out.confidence),
            atr=None,
            extra={
                "engine_regime": engine_out.regime,
                "reason_code": engine_out.reason_code,
                "atr_ratio": engine_out.atr_ratio,
            },
        )


class CoreTechnicalAgentAdapter:
    """Adapts existing core TechnicalAgent to replay `evaluate(...)` protocol."""

    def __init__(
        self,
        technical_agent: TechnicalAgent,
        history_provider: HistoricalOhlcProvider,
    ) -> None:
        self._technical_agent = technical_agent
        self._history_provider = history_provider

    def evaluate(
        self,
        m15_df: pd.DataFrame,
        regime: RegimeOutput,
        symbol: str,
    ) -> TechnicalSignal | None:
        if m15_df.empty or "close" not in m15_df.columns:
            return None

        now_ts = m15_df.index[-1]
        self._history_provider.set_cutoff(now_ts)

        engine_regime = _build_engine_regime_from_replay(regime, now_ts)
        signal = self._technical_agent.evaluate(
            engine_regime,
            TIMEFRAME_M15,
            TIMEFRAME_H1,
            TIMEFRAME_H4,
        )
        if signal is None:
            return None

        return _convert_core_signal_to_replay(signal, m15_df, symbol)


def _build_engine_regime_from_replay(regime: RegimeOutput, now_ts: pd.Timestamp) -> CoreRegimeOutput:
    replay_regime = str(regime.regime or "NO_REGIME").upper()
    engine_regime = _REPLAY_TO_ENGINE_REGIME.get(replay_regime, "NO_TRADE")

    if engine_regime == "TRENDING_BULL":
        trend_state = "BULLISH"
    elif engine_regime == "TRENDING_BEAR":
        trend_state = "BEARISH"
    elif engine_regime == "TRANSITION":
        trend_state = "TRANSITION"
    else:
        trend_state = "FLAT"

    volatility_state = "HIGH" if replay_regime == "HIGH_VOL" else "NORMAL"
    return CoreRegimeOutput(
        regime=engine_regime,
        trend_state=trend_state,
        volatility_state=volatility_state,
        confidence=_clamp01(regime.confidence),
        reason_code="REPLAY_ADAPTER",
        timestamp_utc=now_ts.isoformat(),
        atr_ratio=1.0,
    )


def _convert_core_signal_to_replay(
    signal: CoreTechnicalSignal,
    m15_df: pd.DataFrame,
    symbol: str,
) -> TechnicalSignal | None:
    direction = str(signal.direction).upper()
    if direction not in {"BUY", "SELL"}:
        return None

    entry = float(m15_df["close"].iloc[-1])
    pip = _pip_size(symbol)
    stop_distance = max(float(signal.stop_pips), 0.0) * pip
    tp_distance = max(float(signal.take_profit_pips), 0.0) * pip
    if stop_distance <= 0 or tp_distance <= 0:
        return None

    if direction == "BUY":
        stop_loss = entry - stop_distance
        take_profit = entry + tp_distance
    else:
        stop_loss = entry + stop_distance
        take_profit = entry - tp_distance

    atr_series = calculate_atr(m15_df, 14)
    atr_value = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
    if pd.isna(atr_value) or atr_value <= 0:
        atr_value = stop_distance

    return TechnicalSignal(
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr=atr_value,
        confidence=_clamp01(float(signal.confidence)),
        rule_name=signal.reason_code,
    )


def build_core_agent_replay_adapters(
    symbol: str,
    m15_df: pd.DataFrame,
    h1_df: pd.DataFrame,
    *,
    policy: Mapping[str, object] | None = None,
    spread_pips: float = 1.5,
) -> tuple[CoreRegimeAgentAdapter, CoreTechnicalAgentAdapter, HistoricalOhlcProvider]:
    """Factory for replay-ready adapters backed by existing core agents.

    This function is intended for offline training/replay pipelines only.
    """
    history = HistoricalOhlcProvider(symbol=symbol, m15_df=m15_df, h1_df=h1_df)

    pip = _pip_size(symbol)
    spread_price = max(float(spread_pips), 0.0) * pip
    fetch_spread: Callable[[str], float] = lambda _symbol: spread_price

    regime_agent = RegimeAgent(symbol, history.fetch, policy=policy)
    technical_agent = TechnicalAgent(symbol, history.fetch, fetch_spread=fetch_spread, policy=policy)

    return (
        CoreRegimeAgentAdapter(regime_agent, history),
        CoreTechnicalAgentAdapter(technical_agent, history),
        history,
    )
