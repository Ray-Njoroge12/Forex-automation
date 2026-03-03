from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from core.account_status import AccountStatus
from core.filters.macro_filter import is_macro_aligned
from core.indicators import calculate_atr, calculate_rsi
from core.sentiment.sentiment_agent import SentimentAgent
from core.types import AdversarialDecision, TechnicalSignal

FetchOHLC = Callable[[str, int, int], pd.DataFrame]
FetchSpread = Callable[[str], Optional[float]]


class AdversarialAgent:
    """Attempts to invalidate weak trades before portfolio allocation."""

    MACRO_RISK_MODIFIER = 0.8    # applied when macro opposes direction
    SENTIMENT_RISK_MODIFIER = 0.9  # applied when sentiment strongly opposes direction
    SENTIMENT_THRESHOLD = 0.3       # abs(score) above this triggers the modifier

    def __init__(
        self,
        symbol: str,
        fetch_ohlc: FetchOHLC,
        fetch_spread: FetchSpread,
        rate_differentials: dict[str, float] | None = None,
        sentiment_agent: SentimentAgent | None = None,
    ):
        self.symbol = symbol
        self.fetch_ohlc = fetch_ohlc
        self.fetch_spread = fetch_spread
        self._rate_differentials: dict[str, float] = rate_differentials or {}
        self._sentiment = sentiment_agent
        self.max_spread_pips = 2.0
        self.max_volatility_multiplier = 1.8

    def evaluate(
        self,
        technical_signal: TechnicalSignal,
        account_status: AccountStatus,
        timeframe_m15: int,
    ) -> AdversarialDecision:
        now = datetime.now(timezone.utc).isoformat()

        spread = self.fetch_spread(self.symbol)
        if spread is None:
            return AdversarialDecision(
                approved=False,
                risk_modifier=0.0,
                reason_code="ADV_SPREAD_UNAVAILABLE",
                details="Spread check unavailable",
                timestamp_utc=now,
            )

        pip_value = 0.0001 if "JPY" not in self.symbol else 0.01
        spread_pips = spread / pip_value
        if spread_pips > self.max_spread_pips:
            return AdversarialDecision(
                approved=False,
                risk_modifier=0.0,
                reason_code="ADV_SPREAD_TOO_WIDE",
                details=f"spread_pips={spread_pips:.2f}",
                timestamp_utc=now,
            )

        df = self.fetch_ohlc(self.symbol, timeframe_m15, 120)
        if df.empty:
            return AdversarialDecision(
                approved=False,
                risk_modifier=0.0,
                reason_code="ADV_MARKET_DATA_UNAVAILABLE",
                details="M15 data unavailable",
                timestamp_utc=now,
            )

        df = df.copy()
        df["atr"] = calculate_atr(df, 14)
        df["rsi"] = calculate_rsi(df["close"], 14)

        atr_now = float(df["atr"].iloc[-1]) if not pd.isna(df["atr"].iloc[-1]) else 0.0
        atr_mean = float(df["atr"].rolling(20, min_periods=20).mean().iloc[-1])
        if atr_mean > 0 and atr_now > atr_mean * self.max_volatility_multiplier:
            return AdversarialDecision(
                approved=True,
                risk_modifier=0.6,
                reason_code="ADV_VOLATILITY_EXTREME",
                details=f"atr_now={atr_now:.6f} atr_mean={atr_mean:.6f}",
                timestamp_utc=now,
            )

        last_rsi = float(df["rsi"].iloc[-1]) if not pd.isna(df["rsi"].iloc[-1]) else 50.0
        if technical_signal.direction == "BUY" and last_rsi > 75:
            return AdversarialDecision(
                approved=False,
                risk_modifier=0.0,
                reason_code="ADV_RSI_EXHAUSTION_BUY",
                details=f"rsi={last_rsi:.2f}",
                timestamp_utc=now,
            )
        if technical_signal.direction == "SELL" and last_rsi < 25:
            return AdversarialDecision(
                approved=False,
                risk_modifier=0.0,
                reason_code="ADV_RSI_EXHAUSTION_SELL",
                details=f"rsi={last_rsi:.2f}",
                timestamp_utc=now,
            )

        if account_status.open_usd_exposure_count >= 2:
            return AdversarialDecision(
                approved=False,
                risk_modifier=0.0,
                reason_code="ADV_USD_STACKING",
                details="open_usd_exposure_count>=2",
                timestamp_utc=now,
            )

        # Accumulate soft risk modifiers (macro + sentiment).
        # Both are non-blocking: they reduce position size rather than reject.
        modifier = 1.0
        soft_reason = "ADV_APPROVED"
        soft_details = "No adversarial blocker triggered"

        # Macro: misaligned carry reduces risk by 20%.
        if self._rate_differentials and not is_macro_aligned(
            self.symbol, technical_signal.direction, self._rate_differentials
        ):
            modifier *= self.MACRO_RISK_MODIFIER
            soft_reason = "ADV_MACRO_MISALIGNED"
            soft_details = f"direction={technical_signal.direction} macro_opposes"

        # Sentiment: strong opposition reduces risk by 10%.
        if self._sentiment is not None:
            sent_score = self._sentiment.score(self.symbol)
            buy_opposed = technical_signal.direction == "BUY" and sent_score < -self.SENTIMENT_THRESHOLD
            sell_opposed = technical_signal.direction == "SELL" and sent_score > self.SENTIMENT_THRESHOLD
            if buy_opposed or sell_opposed:
                modifier *= self.SENTIMENT_RISK_MODIFIER
                soft_reason = "ADV_SENTIMENT_OPPOSED" if soft_reason == "ADV_APPROVED" else soft_reason
                soft_details = f"sentiment_score={sent_score:.3f}"

        return AdversarialDecision(
            approved=True,
            risk_modifier=round(modifier, 4),
            reason_code=soft_reason,
            details=soft_details,
            timestamp_utc=now,
        )
