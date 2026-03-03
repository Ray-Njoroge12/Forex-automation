from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RegimeOutput:
    regime: str
    trend_state: str
    volatility_state: str
    confidence: float
    reason_code: str
    timestamp_utc: str
    atr_ratio: float = 1.0  # current ATR / 90-bar mean ATR; 1.0 = neutral


@dataclass(frozen=True)
class TechnicalSignal:
    trade_id: str
    symbol: str
    direction: str
    stop_pips: float
    take_profit_pips: float
    risk_reward: float
    confidence: float
    reason_code: str
    timestamp_utc: str
    rsi_at_entry: float = 0.0  # M15 RSI at signal generation time
    spread_entry: float = 0.0  # spread at signal time in pips


@dataclass(frozen=True)
class AdversarialDecision:
    approved: bool
    risk_modifier: float
    reason_code: str
    details: str
    timestamp_utc: str


@dataclass(frozen=True)
class PortfolioDecision:
    approved: bool
    final_risk_percent: float
    reason_code: str
    details: str
    timestamp_utc: str


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason_code: str
    details: str
    timestamp_utc: str
    risk_throttle_multiplier: float = 1.0  # 1.0 = no throttle; <1.0 = reduce risk


@dataclass(frozen=True)
class TradeProposalRecord:
    symbol: str
    direction: str
    status: str
    reason_code: str
    risk_percent: float
    stop_pips: float
    take_profit_pips: float
    timestamp_utc: datetime
