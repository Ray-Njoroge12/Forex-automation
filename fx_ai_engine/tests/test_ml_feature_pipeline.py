from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.types import TechnicalSignal
from database import db as db_mod


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


def test_ml_migration_adds_all_feature_columns(tmp_path, monkeypatch) -> None:
    db = _patch_db(tmp_path, monkeypatch)
    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_ml_feature_columns()
    with _temp_conn(db) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    expected = {"regime_confidence", "rsi_at_entry", "atr_ratio",
                "is_london_session", "is_newyork_session", "rate_differential", "risk_reward", "rsi_slope"}
    assert expected <= cols


def test_ml_migration_is_idempotent(tmp_path, monkeypatch) -> None:
    _patch_db(tmp_path, monkeypatch)
    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_ml_feature_columns()
    db_mod.migrate_add_ml_feature_columns()  # must not raise


def test_rsi_at_entry_defaults_to_zero_for_backward_compat() -> None:
    sig = TechnicalSignal(
        trade_id="AI_test_compat",
        symbol="GBPUSD", direction="SELL",
        stop_pips=10.0, take_profit_pips=22.0, risk_reward=2.2,
        confidence=0.6, reason_code="TECH_PULLBACK_SELL",
        timestamp_utc="2026-03-01T10:00:00+00:00",
    )
    assert sig.rsi_at_entry == 0.0


def test_insert_trade_proposal_populates_ml_features(tmp_path, monkeypatch) -> None:
    db = _patch_db(tmp_path, monkeypatch)
    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_ml_feature_columns()
    sig = TechnicalSignal(
        trade_id="AI_ml_001", symbol="EURUSD", direction="BUY",
        stop_pips=11.0, take_profit_pips=24.2, risk_reward=2.2,
        confidence=0.74, reason_code="TECH_PULLBACK_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        rsi_at_entry=55.3,
        rsi_slope=2.1,
    )
    db_mod.insert_trade_proposal(
        sig, status="PENDING", reason_code="ROUTED_TO_MT5",
        risk_percent=0.032, market_regime="TRENDING_BULL",
        regime_confidence=0.8, atr_ratio=1.2,
        is_london_session=1, is_newyork_session=0, rate_differential=-2.0,
    )
    with _temp_conn(db) as conn:
        row = conn.execute(
            "SELECT rsi_at_entry, regime_confidence, atr_ratio, is_london_session, "
            "is_newyork_session, rate_differential, risk_reward, rsi_slope "
            "FROM trades WHERE trade_id=?", ("AI_ml_001",),
        ).fetchone()
    assert row is not None
    assert abs(float(row["rsi_at_entry"]) - 55.3) < 0.01
    assert abs(float(row["regime_confidence"]) - 0.8) < 0.01
    assert abs(float(row["atr_ratio"]) - 1.2) < 0.01
    assert int(row["is_london_session"]) == 1
    assert int(row["is_newyork_session"]) == 0
    assert abs(float(row["rate_differential"]) - (-2.0)) < 0.01
    assert abs(float(row["risk_reward"]) - 2.2) < 0.01
    assert abs(float(row["rsi_slope"]) - 2.1) < 0.01


def test_insert_without_ml_kwargs_does_not_raise(tmp_path, monkeypatch) -> None:
    _patch_db(tmp_path, monkeypatch)
    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_ml_feature_columns()
    sig = TechnicalSignal(
        trade_id="AI_ml_002", symbol="USDJPY", direction="SELL",
        stop_pips=8.0, take_profit_pips=17.6, risk_reward=2.2,
        confidence=0.65, reason_code="TECH_PULLBACK_SELL",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(
        sig, status="REJECTED", reason_code="ADV_SPREAD_TOO_WIDE",
        risk_percent=0.0, market_regime="TRENDING_BEAR",
    )  # no ML kwargs — must not raise
