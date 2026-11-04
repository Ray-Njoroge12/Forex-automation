"""Strategy plug-in types (stage D, ADR 0014).

A strategy replaces exactly the Regime and Technical stages of the decision
flow. It reads completed bars only through the injected ``fetch_ohlc`` and
``fetch_spread`` callables and returns a ``StrategyDecision``; it never sizes,
routes or approves anything. Everything downstream (Adversarial, Portfolio
Manager, Hard Risk Engine, routing) is unchanged and runs after it.

The only wall-clock reads in ``core/strategy`` are ``new_trade_id`` and
``utc_now_iso`` below (trade ids and record timestamps, excluded from every
live/replay comparison). Strategy logic takes its clock from the bars.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol
from uuid import uuid4

import pandas as pd

from core.types import RegimeOutput, TechnicalSignal

FetchOHLC = Callable[[str, int, int], pd.DataFrame]  # (symbol, timeframe code, bars) -> closed bars
FetchSpread = Callable[[str], "float | None"]         # price units

# FX_Execution MarketBarsExport writes 400 bars per symbol and timeframe; a
# strategy that asks for more would see different windows live and in replay.
LIVE_BAR_EXPORT_LIMIT = 400

# MT5 timeframe codes (same values as MetaTrader5.TIMEFRAME_* and core.timeframes).
TIMEFRAME_M15 = 15
TIMEFRAME_H1 = 16385
TIMEFRAME_H4 = 16388
TIMEFRAME_D1 = 16408
M15 = pd.Timedelta(minutes=15)

TRENDING_REGIMES = frozenset({"TRENDING_BULL", "TRENDING_BEAR"})
TECH_SKIPPED_REGIME_REJECTED = "TECH_SKIPPED_REGIME_REJECTED"
STRATEGY_ERROR = "STRATEGY_ERROR"


@dataclass(frozen=True)
class StrategyDecision:
    strategy_id: str
    regime: RegimeOutput
    regime_passed: bool          # REGIME funnel outcome PASS/REJECT
    regime_reason_code: str      # REGIME funnel reason
    regime_details: str
    signal: TechnicalSignal | None
    reason_code: str             # TECHNICAL funnel reason
    details: str


class Strategy(Protocol):
    strategy_id: str
    symbol: str

    def evaluate(self) -> StrategyDecision: ...


def new_trade_id() -> str:
    """Trade id in the format TechnicalAgent uses: ``AI_YYYYMMDD_HHMMSS_xxxxxx``."""
    return f"AI_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def utc_now_iso() -> str:
    """Record timestamp (``timestamp_utc`` fields); never used for a trading decision."""
    return datetime.now(timezone.utc).isoformat()


def regime_skip_details(regime: RegimeOutput) -> str:
    """TECHNICAL SKIP details written when the REGIME stage rejects (same text as before stage D)."""
    return f"regime={regime.regime} regime_reason={regime.reason_code} trend_state={regime.trend_state}"


def regime_rejected(strategy_id: str, regime: RegimeOutput, regime_reason_code: str,
                    regime_details: str) -> StrategyDecision:
    return StrategyDecision(
        strategy_id=strategy_id,
        regime=regime,
        regime_passed=False,
        regime_reason_code=regime_reason_code,
        regime_details=regime_details,
        signal=None,
        reason_code=TECH_SKIPPED_REGIME_REJECTED,
        details=regime_skip_details(regime),
    )
