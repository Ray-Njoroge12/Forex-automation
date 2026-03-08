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
        self.last_reason_code = "TECHNICAL_NOT_EVALUATED"
        self.last_details = ""

    def _reject(self, reason_code: str, details: str = "") -> None:
        self.last_reason_code = reason_code
        self.last_details = details

    def _accept(self, reason_code: str, details: str = "") -> None:
        self.last_reason_code = reason_code
        self.last_details = details

    def evaluate(self, regime: RegimeOutput, timeframe_m15: int, timeframe_h1: int, timeframe_h4: int = 16388) -> TechnicalSignal | None:
        if regime.regime not in {"TRENDING_BULL", "TRENDING_BEAR"}:
            self._reject("TECH_REGIME_NOT_TRENDING", f"regime={regime.regime}")
            return None

        # --- Triple Screen: H4 structural gate ---
        h4 = self.fetch_ohlc(self.symbol, timeframe_h4, 350)
        if h4.empty:
            self._reject("TECH_H4_DATA_UNAVAILABLE")
            return None
        h4 = h4.copy()
        h4["ema_fast"] = calculate_ema(h4["close"], self.ema_fast)
        h4["ema_slow"] = calculate_ema(h4["close"], self.ema_slow)
        h4_last = h4.iloc[-1]
        if pd.isna(h4_last["ema_fast"]) or pd.isna(h4_last["ema_slow"]):
            self._reject("TECH_H4_EMA_UNAVAILABLE")
            return None

        h1 = self.fetch_ohlc(self.symbol, timeframe_h1, 350)
        m15 = self.fetch_ohlc(self.symbol, timeframe_m15, 350)
        if h1.empty or m15.empty:
            self._reject("TECH_MARKET_DATA_UNAVAILABLE")
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
            self._reject("TECH_INDICATORS_UNAVAILABLE")
            return None

        pip_value = 0.0001 if "JPY" not in self.symbol else 0.01
        buffer_val = 2.0 * pip_value

        if h1_last["ema_fast"] > h1_last["ema_slow"] and h4_last["ema_fast"] > h4_last["ema_slow"]:
            direction = "BUY"
            # Conservative: Price must pull back near EMA, RSI not overbought
            pulled_back = (m15_last["low"] <= m15_last["ema_fast"] + buffer_val)
            rsi_ok = 40 <= float(m15_last["rsi"]) <= 65
            reason_code = "TECH_CONFIRMED_BUY"
        elif h1_last["ema_fast"] < h1_last["ema_slow"] and h4_last["ema_fast"] < h4_last["ema_slow"]:
            direction = "SELL"
            # Conservative: Price must pull back near EMA, RSI not oversold
            pulled_back = (m15_last["high"] >= m15_last["ema_fast"] - buffer_val)
            rsi_ok = 35 <= float(m15_last["rsi"]) <= 60
            reason_code = "TECH_CONFIRMED_SELL"
        else:
            self._reject("TECH_HIGHER_TIMEFRAME_MISALIGNED")
            return None  # H1 and H4 are not aligned — no signal

        if not pulled_back or not rsi_ok:
            detail = f"pulled_back={pulled_back} rsi_ok={rsi_ok}"
            self._reject("TECH_PULLBACK_OR_RSI_INVALID", detail)
            return None

        # Require structural confirmation (EMA slope alignment and proper close)
        if not self._confirm_candle_pattern(m15, direction):
            self._reject("TECH_CANDLE_CONFIRMATION_FAILED", f"direction={direction}")
            return None

        # Phase 2.4 — dynamic ATR multiplier scaled by regime volatility state.
        atr_multiplier = self._get_atr_multiplier(regime.volatility_state)

        pip_value = 0.0001 if "JPY" not in self.symbol else 0.01
        atr_stop_pips = float((m15_last["atr"] * atr_multiplier) / pip_value)

        # Approach B: snap to structural level if within [0.8×, 1.5×] of ATR stop
        current_price = float(m15_last["close"])
        stop_pips, structural_sl_pips = self._detect_structural_sl(
            m15, direction, atr_stop_pips, current_price
        )
        if stop_pips <= 0:
            self._reject("TECH_STOP_INVALID")
            return None

        # Approach A: resolve regime-driven trade management parameters
        mgmt = self._get_trade_management_params(regime)

        # Spread-adjusted R:R — deduct half the spread from both sides so that
        # the effective stop is wider and the effective TP is narrower.
        live_spread = self.fetch_spread(self.symbol) if self.fetch_spread else None
        spread_pips = (live_spread / pip_value) if live_spread is not None else 1.5
        effective_stop = stop_pips + spread_pips / 2
        min_tp_for_rr = self.min_rr * effective_stop + spread_pips / 2
        # Keep the legacy 2.2x baseline but ensure post-spread effective R:R still meets target.
        take_profit_pips = max(float(stop_pips * 2.2), min_tp_for_rr)
        effective_tp = take_profit_pips - spread_pips / 2
        rr = effective_tp / effective_stop if effective_stop > 0 else 0.0
        if rr < self.min_rr:
            self._reject("TECH_RR_BELOW_MIN", f"rr={rr:.2f} min_rr={self.min_rr:.2f}")
            return None

        # Compute RSI slope (change over last 3 bars) for ML feature.
        rsi_slope = 0.0
        if len(m15) >= 4 and not pd.isna(m15["rsi"].iloc[-1]) and not pd.isna(m15["rsi"].iloc[-3]):
            rsi_slope = round(float(m15["rsi"].iloc[-1]) - float(m15["rsi"].iloc[-3]), 2)

        self._accept(reason_code, f"direction={direction} rr={rr:.2f}")
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
            rsi_slope=rsi_slope,
            be_trigger_r=mgmt["be_trigger_r"],
            partial_close_r=mgmt["partial_close_r"],
            trailing_atr_mult=mgmt["trailing_atr_mult"],
            tp_mode=mgmt["tp_mode"],
            structural_sl_pips=structural_sl_pips,
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

    def _detect_structural_sl(
        self,
        m15: pd.DataFrame,
        direction: str,
        atr_stop_pips: float,
        current_price: float,
    ) -> tuple[float, float | None]:
        """Snap ATR-based stop to nearest swing high/low if within [0.8×, 1.5×] window.

        Returns (final_stop_pips, structural_sl_pips_or_None).
        structural_sl_pips is None when no snap occurred (ATR stop used).
        """
        lookback = 20
        pip_value = 0.0001 if "JPY" not in self.symbol else 0.01
        window = m15.tail(lookback)

        if direction == "BUY":
            structural_level = float(window["low"].min())
            structural_pips = (current_price - structural_level) / pip_value
        else:
            structural_level = float(window["high"].max())
            structural_pips = (structural_level - current_price) / pip_value

        if structural_pips <= 0 or atr_stop_pips <= 0:
            return atr_stop_pips, None

        ratio = structural_pips / atr_stop_pips
        if 0.8 <= ratio <= 1.5:
            return round(structural_pips, 2), round(structural_pips, 2)

        return atr_stop_pips, None

    def _get_trade_management_params(self, regime: "RegimeOutput") -> dict:
        """Map regime + volatility state to per-trade management parameters.

        Returns dict with keys: be_trigger_r, partial_close_r, trailing_atr_mult, tp_mode.
        """
        is_trending = regime.regime in {"TRENDING_BULL", "TRENDING_BEAR"}

        if not is_trending:
            # Ranging / No-Trade: fixed targets, no trailing
            return {
                "be_trigger_r": 1.0,
                "partial_close_r": 0.0,
                "trailing_atr_mult": 0.0,
                "tp_mode": "FIXED",
            }

        # Trending regime — differentiate by volatility
        if regime.volatility_state == "HIGH":
            return {
                "be_trigger_r": 1.2,
                "partial_close_r": 1.5,
                "trailing_atr_mult": 2.0,
                "tp_mode": "TRAIL",
            }
        else:  # NORMAL or LOW
            return {
                "be_trigger_r": 0.8,
                "partial_close_r": 1.2,
                "trailing_atr_mult": 1.5,
                "tp_mode": "TRAIL",
            }
