from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

import pandas as pd

from core.indicators import calculate_adx, calculate_atr, calculate_ema
from core.types import RegimeOutput

FetchOHLC = Callable[[str, int, int], pd.DataFrame]


@dataclass(frozen=True)
class RegimeThresholds:
    ema_fast: int = 50
    ema_slow: int = 200
    atr_period: int = 14
    adx_period: int = 14
    atr_lookback: int = 20
    trend_distance_threshold: float = 0.0005
    adx_no_trade_below: float = 20.0
    adx_transition_below: float = 25.0
    high_vol_atr_ratio: float = 1.25
    low_vol_atr_ratio: float = 0.75
    confidence_adx_cap: float = 30.0

    @classmethod
    def from_policy(cls, policy: Mapping[str, object] | None = None) -> "RegimeThresholds":
        regime_cfg = {}
        if policy:
            thresholds = policy.get("AGENT_THRESHOLDS", {})
            if isinstance(thresholds, Mapping):
                regime_cfg = thresholds.get("REGIME", {})
        if not isinstance(regime_cfg, Mapping):
            regime_cfg = {}
        defaults = cls()
        return cls(
            ema_fast=int(regime_cfg.get("ema_fast", defaults.ema_fast)),
            ema_slow=int(regime_cfg.get("ema_slow", defaults.ema_slow)),
            atr_period=int(regime_cfg.get("atr_period", defaults.atr_period)),
            adx_period=int(regime_cfg.get("adx_period", defaults.adx_period)),
            atr_lookback=int(regime_cfg.get("atr_lookback", defaults.atr_lookback)),
            trend_distance_threshold=float(
                regime_cfg.get("trend_distance_threshold", defaults.trend_distance_threshold)
            ),
            adx_no_trade_below=float(regime_cfg.get("adx_no_trade_below", defaults.adx_no_trade_below)),
            adx_transition_below=float(
                regime_cfg.get("adx_transition_below", defaults.adx_transition_below)
            ),
            high_vol_atr_ratio=float(regime_cfg.get("high_vol_atr_ratio", defaults.high_vol_atr_ratio)),
            low_vol_atr_ratio=float(regime_cfg.get("low_vol_atr_ratio", defaults.low_vol_atr_ratio)),
            confidence_adx_cap=float(regime_cfg.get("confidence_adx_cap", defaults.confidence_adx_cap)),
        )


@dataclass(frozen=True)
class PairSelectiveRisingAdxRelaxation:
    enabled: bool = False
    symbols: tuple[str, ...] = ("EURUSD", "USDJPY")
    adx_lookback_bars: int = 5
    adx_rise_min: float = 1.0
    adx_no_trade_below: float = 18.0
    adx_transition_below: float = 22.0

    @classmethod
    def from_policy(cls, policy: Mapping[str, object] | None = None) -> "PairSelectiveRisingAdxRelaxation":
        cfg = {}
        if policy:
            experiments = policy.get("EXPERIMENTS", {})
            if isinstance(experiments, Mapping):
                cfg = experiments.get("PAIR_SELECTIVE_RISING_ADX_RELAX", {})
        if not isinstance(cfg, Mapping):
            cfg = {}
        defaults = cls()
        raw_symbols = cfg.get("symbols", defaults.symbols)
        if not isinstance(raw_symbols, (list, tuple, set)):
            raw_symbols = defaults.symbols
        return cls(
            enabled=bool(cfg.get("enabled", defaults.enabled)),
            symbols=tuple(str(symbol).strip().upper() for symbol in raw_symbols if str(symbol).strip()),
            adx_lookback_bars=max(int(cfg.get("adx_lookback_bars", defaults.adx_lookback_bars)), 2),
            adx_rise_min=float(cfg.get("adx_rise_min", defaults.adx_rise_min)),
            adx_no_trade_below=float(cfg.get("adx_no_trade_below", defaults.adx_no_trade_below)),
            adx_transition_below=float(
                cfg.get("adx_transition_below", defaults.adx_transition_below)
            ),
        )


class RegimeAgent:
    """Classifies market regime from H1 context."""

    def __init__(
        self,
        symbol: str,
        fetch_ohlc: FetchOHLC,
        *,
        policy: Mapping[str, object] | None = None,
        thresholds: RegimeThresholds | None = None,
    ):
        self.symbol = symbol
        self.fetch_ohlc = fetch_ohlc
        self.thresholds = thresholds or RegimeThresholds.from_policy(policy)
        self.relaxation = PairSelectiveRisingAdxRelaxation.from_policy(policy)
        self.ema_fast = self.thresholds.ema_fast
        self.ema_slow = self.thresholds.ema_slow
        self.atr_period = self.thresholds.atr_period
        self.adx_period = self.thresholds.adx_period
        self.atr_lookback = self.thresholds.atr_lookback
        self.trend_distance_threshold = self.thresholds.trend_distance_threshold
        self.last_reason_code = "REGIME_NOT_EVALUATED"
        self.last_details = ""

    def _set_outcome(self, reason_code: str, details: str = "") -> None:
        self.last_reason_code = reason_code
        self.last_details = details

    def _experiment_is_active(self, adx_series: pd.Series) -> bool:
        if not self.relaxation.enabled or self.symbol.upper() not in self.relaxation.symbols:
            return False
        recent = adx_series.dropna().tail(self.relaxation.adx_lookback_bars)
        if len(recent) < self.relaxation.adx_lookback_bars:
            return False
        return float(recent.iloc[-1] - recent.iloc[0]) >= self.relaxation.adx_rise_min

    def evaluate(self, timeframe_h1: int) -> RegimeOutput:
        df = self.fetch_ohlc(self.symbol, timeframe_h1, 350)
        now = datetime.now(timezone.utc).isoformat()
        if df.empty:
            self._set_outcome("REGIME_DATA_UNAVAILABLE", "ohlc=empty")
            return RegimeOutput(
                regime="NO_TRADE",
                trend_state="UNKNOWN",
                volatility_state="UNKNOWN",
                confidence=0.0,
                reason_code="REGIME_DATA_UNAVAILABLE",
                timestamp_utc=now,
            )

        if len(df) < 5:
            self._set_outcome("REGIME_WARMUP_INCOMPLETE", f"bars={len(df)} required>=5")
            return RegimeOutput(
                regime="NO_TRADE",
                trend_state="UNKNOWN",
                volatility_state="UNKNOWN",
                confidence=0.0,
                reason_code="REGIME_WARMUP_INCOMPLETE",
                timestamp_utc=now,
            )

        df = df.copy()
        df["ema_fast"] = calculate_ema(df["close"], self.ema_fast)
        df["ema_slow"] = calculate_ema(df["close"], self.ema_slow)
        df["atr"] = calculate_atr(df, self.atr_period)
        df["adx"] = calculate_adx(df, self.adx_period)

        current = df.iloc[-1]
        prev = df.iloc[-5]

        if pd.isna(current["ema_fast"]) or pd.isna(current["ema_slow"]) or pd.isna(current["atr"]):
            self._set_outcome("REGIME_WARMUP_INCOMPLETE", "indicators=nan")
            return RegimeOutput(
                regime="NO_TRADE",
                trend_state="UNKNOWN",
                volatility_state="UNKNOWN",
                confidence=0.0,
                reason_code="REGIME_WARMUP_INCOMPLETE",
                timestamp_utc=now,
            )

        trend_distance = abs(current["ema_fast"] - current["ema_slow"])
        atr_mean = df["atr"].rolling(self.atr_lookback, min_periods=self.atr_lookback).mean().iloc[-1]

        # ATR ratio: current ATR relative to 90-bar historical mean (= ~90 hours).
        # Used downstream for volatility-adjusted position sizing.
        atr_90_mean = df["atr"].rolling(90, min_periods=30).mean().iloc[-1]
        if pd.isna(atr_90_mean) or atr_90_mean <= 0:
            atr_ratio = 1.0
        else:
            atr_ratio = round(float(current["atr"]) / float(atr_90_mean), 4)

        trend_state = "FLAT"
        if trend_distance >= self.trend_distance_threshold:
            if current["ema_fast"] > current["ema_slow"] and current["ema_fast"] > prev["ema_fast"]:
                trend_state = "BULLISH"
            elif current["ema_fast"] < current["ema_slow"] and current["ema_fast"] < prev["ema_fast"]:
                trend_state = "BEARISH"
            else:
                trend_state = "TRANSITION"

        # ADX regime gating: gate trend_state on directional movement strength.
        adx_val = float(current["adx"]) if not pd.isna(current["adx"]) else 0.0
        experiment_active = self._experiment_is_active(df["adx"])
        adx_no_trade_below = (
            self.relaxation.adx_no_trade_below if experiment_active else self.thresholds.adx_no_trade_below
        )
        adx_transition_below = (
            self.relaxation.adx_transition_below
            if experiment_active
            else self.thresholds.adx_transition_below
        )
        if adx_val < adx_no_trade_below:
            # No meaningful trend — force to FLAT regardless of EMA position.
            trend_state = "FLAT"
        elif adx_val < adx_transition_below and trend_state in {"BULLISH", "BEARISH"}:
            # Trend is forming but not confirmed — downgrade to TRANSITION.
            trend_state = "TRANSITION"

        if pd.isna(atr_mean) or atr_mean <= 0:
            volatility_state = "NORMAL"
        elif current["atr"] > atr_mean * self.thresholds.high_vol_atr_ratio:
            volatility_state = "HIGH"
        elif current["atr"] < atr_mean * self.thresholds.low_vol_atr_ratio:
            volatility_state = "LOW"
        else:
            volatility_state = "NORMAL"

        if trend_state == "BULLISH":
            regime = "TRENDING_BULL"
            reason_code = "REGIME_TREND_BULL"
            base_confidence = 0.8
        elif trend_state == "BEARISH":
            regime = "TRENDING_BEAR"
            reason_code = "REGIME_TREND_BEAR"
            base_confidence = 0.8
        elif trend_state == "FLAT" and volatility_state == "LOW":
            regime = "RANGING_LOW_VOL"
            reason_code = "REGIME_RANGE_LOW_VOL"
            base_confidence = 0.65
        elif trend_state == "FLAT" and volatility_state == "HIGH":
            regime = "RANGING_HIGH_VOL"
            reason_code = "REGIME_RANGE_HIGH_VOL"
            base_confidence = 0.6
        elif trend_state == "TRANSITION":
            regime = "TRANSITION"
            reason_code = "REGIME_TRANSITION"
            base_confidence = 0.5
        else:
            regime = "NO_TRADE"
            reason_code = "REGIME_NO_TRADE"
            base_confidence = 0.4

        # Scale confidence by ADX strength: capped at base (never inflates above base).
        adx_scale = min(adx_val / self.thresholds.confidence_adx_cap, 1.0)
        confidence = round(base_confidence * adx_scale, 4)

        pip_value = 0.01 if "JPY" in self.symbol else 0.0001
        if current["ema_fast"] > current["ema_slow"]:
            direction_candidate = "BUY"
        elif current["ema_fast"] < current["ema_slow"]:
            direction_candidate = "SELL"
        else:
            direction_candidate = "NONE"

        details = (
            f"regime={regime} trend_state={trend_state} volatility={volatility_state} "
            f"direction_candidate={direction_candidate} close={float(current['close']):.5f} "
            f"ema_fast={float(current['ema_fast']):.5f} ema_slow={float(current['ema_slow']):.5f} "
            f"adx={adx_val:.2f} adx_no_trade_below={adx_no_trade_below:.2f} "
            f"adx_transition_below={adx_transition_below:.2f} "
            f"trend_distance_pips={float(trend_distance) / pip_value:.2f} "
            f"trend_distance_threshold_pips={self.thresholds.trend_distance_threshold / pip_value:.2f} "
            f"atr_ratio={atr_ratio:.4f} experiment_pair_selective_rising_adx_relax={int(experiment_active)}"
        )
        self._set_outcome(reason_code, details)

        return RegimeOutput(
            regime=regime,
            trend_state=trend_state,
            volatility_state=volatility_state,
            confidence=confidence,
            reason_code=reason_code,
            timestamp_utc=now,
            atr_ratio=atr_ratio,
        )
