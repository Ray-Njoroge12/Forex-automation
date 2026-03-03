from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

import pandas as pd

from core.indicators import calculate_atr, calculate_ema, calculate_rsi
from core.types import RegimeOutput, TechnicalSignal

FetchOHLC = Callable[[str, int, int], pd.DataFrame]
FetchSpread = Callable[[str], float]


class TechnicalAgent:
    """Builds M15 trade signal only when regime supports trend continuation."""

    def __init__(self, symbol: str, fetch_ohlc: FetchOHLC, fetch_spread: FetchSpread | None = None):
        self.symbol = symbol
        self.fetch_ohlc = fetch_ohlc
        self.fetch_spread = fetch_spread
        self.ema_fast = 50
        self.ema_slow = 200
        self.atr_period = 14
        self.rsi_period = 14
        self.stop_atr_multiplier = 1.2
        self.min_rr = 2.2

    def evaluate(self, regime: RegimeOutput, timeframe_m15: int, timeframe_h1: int) -> TechnicalSignal | None:
        if regime.regime not in {"TRENDING_BULL", "TRENDING_BEAR"}:
            return None

        h1 = self.fetch_ohlc(self.symbol, timeframe_h1, 350)
        m15 = self.fetch_ohlc(self.symbol, timeframe_m15, 350)
        if h1.empty or m15.empty:
            return None

        h1 = h1.copy()
        m15 = m15.copy()

        h1["ema_fast"] = calculate_ema(h1["close"], self.ema_fast)
        h1["ema_slow"] = calculate_ema(h1["close"], self.ema_slow)
        m15["ema_fast"] = calculate_ema(m15["close"], self.ema_fast)
        m15["atr"] = calculate_atr(m15, self.atr_period)
        m15["rsi"] = calculate_rsi(m15["close"], self.rsi_period)

        h1_last = h1.iloc[-1]
        m15_last = m15.iloc[-1]

        if any(pd.isna(v) for v in [h1_last["ema_fast"], h1_last["ema_slow"], m15_last["atr"], m15_last["rsi"]]):
            return None

        if h1_last["ema_fast"] > h1_last["ema_slow"]:
            direction = "BUY"
            pulled_back = m15_last["low"] <= m15_last["ema_fast"]
            rsi_ok = 40 <= float(m15_last["rsi"]) <= 65
            reason_code = "TECH_PULLBACK_BUY"
        elif h1_last["ema_fast"] < h1_last["ema_slow"]:
            direction = "SELL"
            pulled_back = m15_last["high"] >= m15_last["ema_fast"]
            rsi_ok = 35 <= float(m15_last["rsi"]) <= 60
            reason_code = "TECH_PULLBACK_SELL"
        else:
            return None

        if not pulled_back or not rsi_ok:
            return None

        # Phase 2.3 — Option C candle pattern confirmation: EMA slope + body close.
        if not self._confirm_candle_pattern(m15, direction):
            return None

        # Phase 2.4 — dynamic ATR multiplier scaled by regime volatility state.
        atr_multiplier = self._get_atr_multiplier(regime.volatility_state)

        pip_value = 0.0001 if "JPY" not in self.symbol else 0.01
        stop_pips = float((m15_last["atr"] * atr_multiplier) / pip_value)
        take_profit_pips = float(stop_pips * self.min_rr)
        if stop_pips <= 0 or take_profit_pips <= 0:
            return None

        # Spread-adjusted R:R — deduct half the spread from both sides so that
        # the effective stop is wider and the effective TP is narrower.
        live_spread = self.fetch_spread(self.symbol) if self.fetch_spread else None
        spread_pips = (live_spread / pip_value) if live_spread is not None else 1.5
        effective_stop = stop_pips + spread_pips / 2
        effective_tp = take_profit_pips - spread_pips / 2
        rr = effective_tp / effective_stop if effective_stop > 0 else 0.0
        if rr < self.min_rr:
            return None

        return TechnicalSignal(
            trade_id=f"AI_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}",
            symbol=self.symbol,
            direction=direction,
            stop_pips=round(stop_pips, 2),
            take_profit_pips=round(take_profit_pips, 2),
            risk_reward=round(rr, 2),
            confidence=0.72,
            reason_code=reason_code,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            rsi_at_entry=round(float(m15_last["rsi"]), 2),
            spread_entry=round(spread_pips, 2),
        )

    def _confirm_candle_pattern(self, m15: pd.DataFrame, direction: str) -> bool:
        """Option C — EMA slope + body close confirmation.

        BUY: EMA(50) slope is rising (ema[-1] > ema[-3]) AND
             current candle close is above EMA(50).
        SELL: EMA(50) slope is falling AND close is below EMA(50).

        Requires at least 4 M15 bars with non-NaN EMA(50).
        """
        if len(m15) < 4 or "ema_fast" not in m15.columns:
            return False

        ema = m15["ema_fast"]
        if pd.isna(ema.iloc[-1]) or pd.isna(ema.iloc[-3]):
            return False

        slope_up = float(ema.iloc[-1]) > float(ema.iloc[-3])
        close = float(m15["close"].iloc[-1])
        ema_now = float(ema.iloc[-1])

        if direction == "BUY":
            return slope_up and close > ema_now
        else:
            return (not slope_up) and close < ema_now

    @staticmethod
    def _get_atr_multiplier(volatility_state: str) -> float:
        """Widen stops in high volatility, tighten in low — preserves R:R edge."""
        return {"HIGH": 1.5, "NORMAL": 1.2, "LOW": 1.0}.get(volatility_state, 1.2)
