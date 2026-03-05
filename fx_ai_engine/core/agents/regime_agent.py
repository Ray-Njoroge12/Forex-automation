from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from core.indicators import calculate_adx, calculate_atr, calculate_ema
from core.types import RegimeOutput

FetchOHLC = Callable[[str, int, int], pd.DataFrame]


class RegimeAgent:
    """Classifies market regime from H1 context."""

    def __init__(self, symbol: str, fetch_ohlc: FetchOHLC):
        self.symbol = symbol
        self.fetch_ohlc = fetch_ohlc
        self.ema_fast = 50
        self.ema_slow = 200
        self.atr_period = 14
        self.adx_period = 14
        self.atr_lookback = 20
        self.trend_distance_threshold = 0.0005

    def evaluate(self, timeframe_h1: int) -> RegimeOutput:
        df = self.fetch_ohlc(self.symbol, timeframe_h1, 350)
        now = datetime.now(timezone.utc).isoformat()
        if df.empty:
            return RegimeOutput(
                regime="NO_TRADE",
                trend_state="UNKNOWN",
                volatility_state="UNKNOWN",
                confidence=0.0,
                reason_code="REGIME_DATA_UNAVAILABLE",
                timestamp_utc=now,
            )

        if len(df) < 5:
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
        if adx_val < 20:
            # No meaningful trend — force to FLAT regardless of EMA position.
            trend_state = "FLAT"
        elif adx_val < 25 and trend_state in {"BULLISH", "BEARISH"}:
            # Trend is forming but not confirmed — downgrade to TRANSITION.
            trend_state = "TRANSITION"

        if pd.isna(atr_mean) or atr_mean <= 0:
            volatility_state = "NORMAL"
        elif current["atr"] > atr_mean * 1.25:
            volatility_state = "HIGH"
        elif current["atr"] < atr_mean * 0.75:
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
        adx_scale = min(adx_val / 30.0, 1.0)
        confidence = round(base_confidence * adx_scale, 4)

        return RegimeOutput(
            regime=regime,
            trend_state=trend_state,
            volatility_state=volatility_state,
            confidence=confidence,
            reason_code=reason_code,
            timestamp_utc=now,
            atr_ratio=atr_ratio,
        )
