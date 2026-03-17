from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping
from uuid import uuid4

import pandas as pd

from core.indicators import calculate_atr, calculate_ema, calculate_rsi
from core.types import RegimeOutput, TechnicalSignal

FetchOHLC = Callable[[str, int, int], pd.DataFrame]
FetchSpread = Callable[[str], float]


@dataclass(frozen=True)
class TechnicalThresholds:
    ema_fast: int = 50
    ema_slow: int = 200
    atr_period: int = 14
    rsi_period: int = 14
    stop_atr_multiplier: float = 1.2
    min_rr: float = 2.2
    pullback_buffer_pips: float = 2.0
    buy_rsi_min: float = 40.0
    buy_rsi_max: float = 65.0
    sell_rsi_min: float = 35.0
    sell_rsi_max: float = 60.0
    structural_lookback: int = 20
    structural_ratio_min: float = 0.8
    structural_ratio_max: float = 1.5

    @classmethod
    def from_policy(cls, policy: Mapping[str, object] | None = None) -> "TechnicalThresholds":
        technical_cfg = {}
        if policy:
            thresholds = policy.get("AGENT_THRESHOLDS", {})
            if isinstance(thresholds, Mapping):
                technical_cfg = thresholds.get("TECHNICAL", {})
        if not isinstance(technical_cfg, Mapping):
            technical_cfg = {}
        defaults = cls()
        return cls(
            ema_fast=int(technical_cfg.get("ema_fast", defaults.ema_fast)),
            ema_slow=int(technical_cfg.get("ema_slow", defaults.ema_slow)),
            atr_period=int(technical_cfg.get("atr_period", defaults.atr_period)),
            rsi_period=int(technical_cfg.get("rsi_period", defaults.rsi_period)),
            stop_atr_multiplier=float(
                technical_cfg.get("stop_atr_multiplier", defaults.stop_atr_multiplier)
            ),
            min_rr=float(policy.get("MIN_RISK_REWARD", defaults.min_rr)) if policy else defaults.min_rr,
            pullback_buffer_pips=float(
                technical_cfg.get("pullback_buffer_pips", defaults.pullback_buffer_pips)
            ),
            buy_rsi_min=float(technical_cfg.get("buy_rsi_min", defaults.buy_rsi_min)),
            buy_rsi_max=float(technical_cfg.get("buy_rsi_max", defaults.buy_rsi_max)),
            sell_rsi_min=float(technical_cfg.get("sell_rsi_min", defaults.sell_rsi_min)),
            sell_rsi_max=float(technical_cfg.get("sell_rsi_max", defaults.sell_rsi_max)),
            structural_lookback=int(
                technical_cfg.get("structural_lookback", defaults.structural_lookback)
            ),
            structural_ratio_min=float(
                technical_cfg.get("structural_ratio_min", defaults.structural_ratio_min)
            ),
            structural_ratio_max=float(
                technical_cfg.get("structural_ratio_max", defaults.structural_ratio_max)
            ),
        )


@dataclass(frozen=True)
class AudusdPullbackRelaxation:
    enabled: bool = False
    symbols: tuple[str, ...] = ("AUDUSD",)
    pullback_buffer_pips: float = 4.0

    @classmethod
    def from_policy(cls, policy: Mapping[str, object] | None = None) -> "AudusdPullbackRelaxation":
        cfg = {}
        if policy:
            experiments = policy.get("EXPERIMENTS", {})
            if isinstance(experiments, Mapping):
                cfg = experiments.get("AUDUSD_PULLBACK_RELAX", {})
        if not isinstance(cfg, Mapping):
            cfg = {}
        defaults = cls()
        raw_symbols = cfg.get("symbols", defaults.symbols)
        if not isinstance(raw_symbols, (list, tuple, set)):
            raw_symbols = defaults.symbols
        return cls(
            enabled=bool(cfg.get("enabled", defaults.enabled)),
            symbols=tuple(str(symbol).strip().upper() for symbol in raw_symbols if str(symbol).strip()),
            pullback_buffer_pips=float(cfg.get("pullback_buffer_pips", defaults.pullback_buffer_pips)),
        )


@dataclass(frozen=True)
class LiveTradeManagementOptionC:
    enabled: bool = False
    low_normal_be_trigger_r: float = 0.5
    low_normal_partial_close_r: float = 1.0
    low_normal_trailing_atr_mult: float = 1.5
    low_normal_tp_mode: str = "HYBRID"
    high_be_trigger_r: float = 0.75
    high_partial_close_r: float = 1.25
    high_trailing_atr_mult: float = 2.0
    high_tp_mode: str = "HYBRID"

    @classmethod
    def from_policy(cls, policy: Mapping[str, object] | None = None) -> "LiveTradeManagementOptionC":
        cfg = {}
        if policy:
            experiments = policy.get("EXPERIMENTS", {})
            if isinstance(experiments, Mapping):
                cfg = experiments.get("LIVE_TRADE_MGMT_OPTION_C", {})
        if not isinstance(cfg, Mapping):
            cfg = {}
        defaults = cls()
        low_normal_cfg = cfg.get("low_normal", {})
        if not isinstance(low_normal_cfg, Mapping):
            low_normal_cfg = {}
        high_cfg = cfg.get("high", {})
        if not isinstance(high_cfg, Mapping):
            high_cfg = {}
        return cls(
            enabled=bool(cfg.get("enabled", defaults.enabled)),
            low_normal_be_trigger_r=float(
                low_normal_cfg.get("be_trigger_r", defaults.low_normal_be_trigger_r)
            ),
            low_normal_partial_close_r=float(
                low_normal_cfg.get("partial_close_r", defaults.low_normal_partial_close_r)
            ),
            low_normal_trailing_atr_mult=float(
                low_normal_cfg.get("trailing_atr_mult", defaults.low_normal_trailing_atr_mult)
            ),
            low_normal_tp_mode=str(low_normal_cfg.get("tp_mode", defaults.low_normal_tp_mode) or defaults.low_normal_tp_mode),
            high_be_trigger_r=float(high_cfg.get("be_trigger_r", defaults.high_be_trigger_r)),
            high_partial_close_r=float(high_cfg.get("partial_close_r", defaults.high_partial_close_r)),
            high_trailing_atr_mult=float(
                high_cfg.get("trailing_atr_mult", defaults.high_trailing_atr_mult)
            ),
            high_tp_mode=str(high_cfg.get("tp_mode", defaults.high_tp_mode) or defaults.high_tp_mode),
        )


class TechnicalAgent:
    """Builds M15 trade signal only when regime supports trend continuation."""

    def __init__(
        self,
        symbol: str,
        fetch_ohlc: FetchOHLC,
        fetch_spread: FetchSpread | None = None,
        *,
        policy: Mapping[str, object] | None = None,
        thresholds: TechnicalThresholds | None = None,
    ):
        self.symbol = symbol
        self.fetch_ohlc = fetch_ohlc
        self.fetch_spread = fetch_spread
        self.thresholds = thresholds or TechnicalThresholds.from_policy(policy)
        self.pullback_relaxation = AudusdPullbackRelaxation.from_policy(policy)
        self.trade_management_option_c = LiveTradeManagementOptionC.from_policy(policy)
        self.ema_fast = self.thresholds.ema_fast
        self.ema_slow = self.thresholds.ema_slow
        self.atr_period = self.thresholds.atr_period
        self.rsi_period = self.thresholds.rsi_period
        self.stop_atr_multiplier = self.thresholds.stop_atr_multiplier
        self.min_rr = self.thresholds.min_rr
        self.pullback_buffer_pips = self.thresholds.pullback_buffer_pips
        self.last_reason_code = "TECHNICAL_NOT_EVALUATED"
        self.last_details = ""

    def _pullback_experiment_is_active(self) -> bool:
        return self.pullback_relaxation.enabled and self.symbol.upper() in self.pullback_relaxation.symbols

    def _pullback_gap_pips(
        self,
        *,
        direction: str,
        m15_last: pd.Series,
        buffer_val: float,
        pip_value: float,
    ) -> float:
        if direction == "BUY":
            return max(float(m15_last["low"]) - float(m15_last["ema_fast"] + buffer_val), 0.0) / pip_value
        return max(float(m15_last["ema_fast"] - buffer_val) - float(m15_last["high"]), 0.0) / pip_value

    def _reject(self, reason_code: str, details: str = "") -> None:
        self.last_reason_code = reason_code
        self.last_details = details

    def _accept(self, reason_code: str, details: str = "") -> None:
        self.last_reason_code = reason_code
        self.last_details = details

    def evaluate(self, regime: RegimeOutput, timeframe_m15: int, timeframe_h1: int, timeframe_h4: int = 16388) -> TechnicalSignal | None:
        if regime.regime not in {"TRENDING_BULL", "TRENDING_BEAR"}:
            self._reject(
                "TECH_REGIME_NOT_TRENDING",
                f"regime={regime.regime} regime_reason={regime.reason_code} trend_state={regime.trend_state}",
            )
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
        experiment_active = self._pullback_experiment_is_active()
        effective_pullback_buffer_pips = (
            self.pullback_relaxation.pullback_buffer_pips if experiment_active else self.pullback_buffer_pips
        )
        buffer_val = effective_pullback_buffer_pips * pip_value

        if h1_last["ema_fast"] > h1_last["ema_slow"] and h4_last["ema_fast"] > h4_last["ema_slow"]:
            direction = "BUY"
            # Conservative: Price must pull back near EMA, RSI not overbought
            pulled_back = (m15_last["low"] <= m15_last["ema_fast"] + buffer_val)
            rsi_floor = self.thresholds.buy_rsi_min
            rsi_ceiling = self.thresholds.buy_rsi_max
            rsi_ok = rsi_floor <= float(m15_last["rsi"]) <= rsi_ceiling
            reason_code = "TECH_CONFIRMED_BUY"
        elif h1_last["ema_fast"] < h1_last["ema_slow"] and h4_last["ema_fast"] < h4_last["ema_slow"]:
            direction = "SELL"
            # Conservative: Price must pull back near EMA, RSI not oversold
            pulled_back = (m15_last["high"] >= m15_last["ema_fast"] - buffer_val)
            rsi_floor = self.thresholds.sell_rsi_min
            rsi_ceiling = self.thresholds.sell_rsi_max
            rsi_ok = rsi_floor <= float(m15_last["rsi"]) <= rsi_ceiling
            reason_code = "TECH_CONFIRMED_SELL"
        else:
            self._reject(
                "TECH_HIGHER_TIMEFRAME_MISALIGNED",
                (
                    f"h1_ema_fast={float(h1_last['ema_fast']):.5f} h1_ema_slow={float(h1_last['ema_slow']):.5f} "
                    f"h4_ema_fast={float(h4_last['ema_fast']):.5f} h4_ema_slow={float(h4_last['ema_slow']):.5f}"
                ),
            )
            return None  # H1 and H4 are not aligned — no signal

        if not pulled_back or not rsi_ok:
            pullback_gap_pips = self._pullback_gap_pips(
                direction=direction,
                m15_last=m15_last,
                buffer_val=buffer_val,
                pip_value=pip_value,
            )
            detail = (
                f"direction={direction} pulled_back={pulled_back} rsi_ok={rsi_ok} "
                f"rsi={float(m15_last['rsi']):.2f} rsi_floor={rsi_floor:.2f} rsi_ceiling={rsi_ceiling:.2f} "
                f"pullback_gap_pips={pullback_gap_pips:.2f} buffer_pips={effective_pullback_buffer_pips:.2f} "
                f"experiment_audusd_pullback_relax={int(experiment_active)} "
                f"close={float(m15_last['close']):.5f} ema50={float(m15_last['ema_fast']):.5f}"
            )
            self._reject("TECH_PULLBACK_OR_RSI_INVALID", detail)
            return None

        # Require structural confirmation (EMA slope alignment and proper close)
        if not self._confirm_candle_pattern(m15, direction):
            self._reject(
                "TECH_CANDLE_CONFIRMATION_FAILED",
                (
                    f"direction={direction} close={float(m15_last['close']):.5f} "
                    f"ema50={float(m15_last['ema_fast']):.5f} ema50_prev3={float(m15['ema_fast'].iloc[-3]):.5f}"
                ),
            )
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
        # Keep the configured minimum-R baseline while ensuring post-spread effective R:R still meets target.
        take_profit_pips = max(float(stop_pips * self.min_rr), min_tp_for_rr)
        effective_tp = take_profit_pips - spread_pips / 2
        rr = effective_tp / effective_stop if effective_stop > 0 else 0.0
        if rr < self.min_rr:
            self._reject("TECH_RR_BELOW_MIN", f"rr={rr:.2f} min_rr={self.min_rr:.2f}")
            return None

        # Compute RSI slope (change over last 3 bars) for ML feature.
        rsi_slope = 0.0
        if len(m15) >= 4 and not pd.isna(m15["rsi"].iloc[-1]) and not pd.isna(m15["rsi"].iloc[-3]):
            rsi_slope = round(float(m15["rsi"].iloc[-1]) - float(m15["rsi"].iloc[-3]), 2)

        pullback_gap_pips = self._pullback_gap_pips(
            direction=direction,
            m15_last=m15_last,
            buffer_val=buffer_val,
            pip_value=pip_value,
        )
        self._accept(
            reason_code,
            (
                f"direction={direction} rr={rr:.2f} spread_pips={spread_pips:.2f} "
                f"stop_pips={stop_pips:.2f} tp_pips={take_profit_pips:.2f} "
                f"rsi={float(m15_last['rsi']):.2f} pullback_gap_pips={pullback_gap_pips:.2f} "
                f"buffer_pips={effective_pullback_buffer_pips:.2f} "
                f"experiment_audusd_pullback_relax={int(experiment_active)}"
            ),
        )
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
        lookback = self.thresholds.structural_lookback
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
        if self.thresholds.structural_ratio_min <= ratio <= self.thresholds.structural_ratio_max:
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

        if self.trade_management_option_c.enabled:
            if regime.volatility_state == "HIGH":
                return {
                    "be_trigger_r": self.trade_management_option_c.high_be_trigger_r,
                    "partial_close_r": self.trade_management_option_c.high_partial_close_r,
                    "trailing_atr_mult": self.trade_management_option_c.high_trailing_atr_mult,
                    "tp_mode": self.trade_management_option_c.high_tp_mode,
                }
            return {
                "be_trigger_r": self.trade_management_option_c.low_normal_be_trigger_r,
                "partial_close_r": self.trade_management_option_c.low_normal_partial_close_r,
                "trailing_atr_mult": self.trade_management_option_c.low_normal_trailing_atr_mult,
                "tp_mode": self.trade_management_option_c.low_normal_tp_mode,
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
