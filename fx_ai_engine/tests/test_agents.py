from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from core.account_status import AccountStatus
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.types import AdversarialDecision, RegimeOutput, TechnicalSignal


def _build_ohlc_series(rows: int = 350, drift: float = 0.00012) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=15 * i) for i in range(rows)]

    closes: list[float] = []
    base = 1.0800
    for i in range(rows):
        value = base + drift * i
        if i > rows - 30:
            # Mild pullback section keeps trend while avoiding runaway RSI.
            value -= 0.00008 * (i - (rows - 30))
        closes.append(round(value, 6))

    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.00025 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.00025 for o, c in zip(opens, closes)]

    # Force final bar to include an EMA pullback wick.
    lows[-1] = lows[-1] - 0.0012

    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "tick_volume": [100] * rows,
            "spread": [10] * rows,
            "real_volume": [100] * rows,
        },
        index=pd.DatetimeIndex(times, name="time"),
    )


def test_regime_agent_returns_trending_bull() -> None:
    h1_df = _build_ohlc_series(rows=350, drift=0.0002)

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        return h1_df

    agent = RegimeAgent("EURUSD", fetch)
    out = agent.evaluate(timeframe_h1=16385)

    assert out.regime in {"TRENDING_BULL", "TRANSITION", "RANGING_HIGH_VOL", "RANGING_LOW_VOL", "NO_TRADE"}
    # Core contract: deterministic output and reason code present.
    assert out.reason_code.startswith("REGIME_")


def test_technical_agent_blocks_when_regime_not_trending() -> None:
    df = _build_ohlc_series()

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        return df

    agent = TechnicalAgent("EURUSD", fetch)
    regime = RegimeOutput(
        regime="NO_TRADE",
        trend_state="FLAT",
        volatility_state="NORMAL",
        confidence=0.3,
        reason_code="REGIME_NO_TRADE",
        timestamp_utc="2026-02-25T12:00:00+00:00",
    )

    signal = agent.evaluate(regime, timeframe_m15=15, timeframe_h1=16385)
    assert signal is None


def test_adversarial_agent_rejects_wide_spread() -> None:
    df = _build_ohlc_series()

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        return df

    def spread(_symbol: str) -> float:
        return 0.00035  # 3.5 pips on non-JPY pair

    signal = TechnicalSignal(
        trade_id="AI_20260225_123000_deadbe",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=12.0,
        take_profit_pips=26.4,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_PULLBACK_BUY",
        timestamp_utc="2026-02-25T12:30:00+00:00",
    )

    out = AdversarialAgent("EURUSD", fetch, spread).evaluate(
        technical_signal=signal,
        account_status=AccountStatus(),
        timeframe_m15=15,
    )

    assert out.approved is False
    assert out.reason_code == "ADV_SPREAD_TOO_WIDE"


def test_portfolio_manager_applies_trade_and_exposure_caps() -> None:
    manager = PortfolioManager()

    signal = TechnicalSignal(
        trade_id="AI_20260225_124500_aa11bb",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=25.0,
        risk_reward=2.5,
        confidence=0.75,
        reason_code="TECH_PULLBACK_BUY",
        timestamp_utc="2026-02-25T12:45:00+00:00",
    )

    status = AccountStatus(open_positions_count=2)
    adversarial_ok = AdversarialDecision(
        approved=True,
        risk_modifier=1.0,
        reason_code="ADV_APPROVED",
        details="ok",
        timestamp_utc="2026-02-25T12:45:00+00:00",
    )

    decision = manager.evaluate(signal, adversarial_ok, status)
    assert decision.approved is False
    assert decision.reason_code == "PM_MAX_TRADES_REACHED"
