"""Integration tests: full agent pipeline produces consistent typed outputs."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from core.account_status import AccountStatus
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.risk.hard_risk_engine import HardRiskEngine
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15
import database.db as db_mod


@contextmanager
def _temp_conn(temp_db: Path):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _patch_db(tmp_path, monkeypatch) -> Path:
    temp_db = tmp_path / "trading_state.db"
    temp_schema = tmp_path / "schema.sql"
    temp_schema.write_text(db_mod.SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(db_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(db_mod, "SCHEMA_PATH", temp_schema)
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))
    return temp_db


def _ohlc(rows: int = 350, drift: float = 0.0002) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=15 * i) for i in range(rows)]
    closes = [round(1.0800 + drift * i, 6) for i in range(rows)]
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.00025 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.00025 for o, c in zip(opens, closes)]
    lows[-1] -= 0.0012
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "tick_volume": [100] * rows, "spread": [10] * rows, "real_volume": [100] * rows},
        index=pd.DatetimeIndex(times, name="time"),
    )


def test_regime_output_carries_atr_ratio() -> None:
    """RegimeOutput.atr_ratio is populated and in a plausible range."""
    df = _ohlc()
    agent = RegimeAgent("EURUSD", lambda *_: df)
    out = agent.evaluate(TIMEFRAME_H1)
    assert isinstance(out.atr_ratio, float)
    assert 0.0 < out.atr_ratio < 10.0


def test_technical_signal_carries_rsi_at_entry() -> None:
    """TechnicalSignal.rsi_at_entry is populated when signal is generated."""
    df = _ohlc(drift=0.0002)
    regime_agent = RegimeAgent("EURUSD", lambda *_: df)
    tech_agent = TechnicalAgent("EURUSD", lambda *_: df, lambda _: 0.00010)
    regime = regime_agent.evaluate(TIMEFRAME_H1)
    signal = tech_agent.evaluate(regime, TIMEFRAME_M15, TIMEFRAME_H1)
    if signal is not None:
        assert isinstance(signal.rsi_at_entry, float)
        assert 0.0 <= signal.rsi_at_entry <= 100.0


def test_pipeline_produces_consistent_types() -> None:
    """Full pipeline regime->technical->adversarial->portfolio->risk produces typed outputs."""
    df = _ohlc(drift=0.0002)
    fetch = lambda *_: df
    spread = lambda _: 0.00010
    regime_agent = RegimeAgent("EURUSD", fetch)
    tech_agent = TechnicalAgent("EURUSD", fetch, spread)
    adv_agent = AdversarialAgent("EURUSD", fetch, spread)
    pm = PortfolioManager()
    risk = HardRiskEngine()
    account = AccountStatus()

    regime = regime_agent.evaluate(TIMEFRAME_H1)
    assert regime.reason_code.startswith("REGIME_")
    technical = tech_agent.evaluate(regime, TIMEFRAME_M15, TIMEFRAME_H1)

    if technical is not None:
        assert technical.risk_reward >= 2.2
        assert technical.direction in {"BUY", "SELL"}
        adversarial = adv_agent.evaluate(technical, account, TIMEFRAME_M15)
        assert 0.0 < adversarial.risk_modifier <= 1.0
        portfolio = pm.evaluate(technical, adversarial, account, regime=regime)
        assert isinstance(portfolio.approved, bool)
        if portfolio.approved:
            decision = risk.validate(account, portfolio.final_risk_percent)
            assert isinstance(decision.approved, bool)
            assert 0.0 < decision.risk_throttle_multiplier <= 1.0


def test_risk_engine_halts_on_daily_stop() -> None:
    account = AccountStatus(daily_loss_percent=0.08)
    decision = HardRiskEngine().validate(account, proposed_risk_percent=0.032)
    assert decision.approved is False
    assert decision.reason_code == "RISK_DAILY_STOP"


def test_risk_engine_halts_on_drawdown() -> None:
    account = AccountStatus(drawdown_percent=0.20)
    decision = HardRiskEngine().validate(account, proposed_risk_percent=0.032)
    assert decision.approved is False
    assert decision.reason_code == "RISK_DRAWDOWN_STOP"


def test_ml_features_write_to_db_on_pending_trade(tmp_path, monkeypatch) -> None:
    """When a trade is routed (PENDING), ML features are persisted in the trades table."""
    db = _patch_db(tmp_path, monkeypatch)
    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_ml_feature_columns()

    from core.types import TechnicalSignal
    sig = TechnicalSignal(
        trade_id="AI_integration_001",
        symbol="EURUSD", direction="BUY",
        stop_pips=12.0, take_profit_pips=26.4, risk_reward=2.2,
        confidence=0.74, reason_code="TECH_PULLBACK_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        rsi_at_entry=57.1,
        spread_entry=1.2,
    )
    db_mod.insert_trade_proposal(
        sig, status="PENDING", reason_code="ROUTED_TO_MT5",
        risk_percent=0.032, market_regime="TRENDING_BULL",
        regime_confidence=0.75, atr_ratio=0.9,
        is_london_session=1, is_newyork_session=0, rate_differential=-2.0,
    )

    with _temp_conn(db) as conn:
        row = conn.execute(
            "SELECT rsi_at_entry, regime_confidence, is_london_session "
            "FROM trades WHERE trade_id=?",
            ("AI_integration_001",),
        ).fetchone()
    assert row is not None
    assert abs(float(row["rsi_at_entry"]) - 57.1) < 0.01
    assert int(row["is_london_session"]) == 1
