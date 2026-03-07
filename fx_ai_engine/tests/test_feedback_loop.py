"""Tests for execution feedback consumption and account state updates."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import database.db as db_mod
from bridge.execution_feedback import ExecutionFeedbackReader
from core.account_status import AccountStatus
from core.types import TechnicalSignal


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


def test_winning_trade_resets_consecutive_losses(tmp_path) -> None:
    """After a winning feedback payload, consecutive_losses resets to 0."""
    feedback_dir = tmp_path / "feedback"
    feedback_dir.mkdir(parents=True)
    payload = {
        "trade_id": "AI_fb_win_001", "ticket": 10001, "status": "CLOSED",
        "entry_price": 1.1000, "slippage": 0.00001, "spread_at_entry": 0.0001,
        "profit_loss": 25.0, "r_multiple": 2.4,
        "close_time": datetime.now(timezone.utc).isoformat(),
    }
    (feedback_dir / "execution_win.json").write_text(json.dumps(payload), encoding="utf-8")
    account = AccountStatus(consecutive_losses=2)
    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir)
    for fb in reader.consume_execution_feedback():
        pnl = float(fb.get("profit_loss", 0.0))
        if pnl > 0:
            account.consecutive_losses = 0
    assert account.consecutive_losses == 0


def test_losing_trade_increments_consecutive_losses(tmp_path) -> None:
    """After a losing feedback payload, consecutive_losses increments."""
    feedback_dir = tmp_path / "feedback"
    feedback_dir.mkdir(parents=True)
    payload = {
        "trade_id": "AI_fb_loss_001", "ticket": 10002, "status": "CLOSED",
        "entry_price": 1.1000, "slippage": 0.00001, "spread_at_entry": 0.0001,
        "profit_loss": -15.0, "r_multiple": -1.0,
        "close_time": datetime.now(timezone.utc).isoformat(),
    }
    (feedback_dir / "execution_loss.json").write_text(json.dumps(payload), encoding="utf-8")
    account = AccountStatus(consecutive_losses=1)
    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir)
    for fb in reader.consume_execution_feedback():
        pnl = float(fb.get("profit_loss", 0.0))
        if pnl < 0:
            account.consecutive_losses += 1
    assert account.consecutive_losses == 2


def test_feedback_updates_trade_status_in_db(tmp_path, monkeypatch) -> None:
    """Execution feedback opens trade; exit feedback closes it with final PnL/R."""
    db = _patch_db(tmp_path, monkeypatch)
    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_ml_feature_columns()

    sig = TechnicalSignal(
        trade_id="AI_fb_db_001",
        symbol="GBPUSD", direction="SELL",
        stop_pips=10.0, take_profit_pips=22.0, risk_reward=2.2,
        confidence=0.65, reason_code="TECH_PULLBACK_SELL",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(
        sig, status="PENDING", reason_code="ROUTED_TO_MT5",
        risk_percent=0.032, market_regime="TRENDING_BEAR",
    )
    db_mod.update_trade_execution_result({
        "trade_id": "AI_fb_db_001", "ticket": 99001, "status": "EXECUTED",
        "entry_price": 1.2650, "slippage": 0.00002, "spread_at_entry": 0.00012,
        "profit_loss": 0.0, "r_multiple": 0.0,
        "close_time": datetime.now(timezone.utc).isoformat(),
    })
    db_mod.update_trade_exit_result({
        "ticket": 99001,
        "status": "CLOSED_WIN",
        "profit_loss": 18.5,
        "r_multiple": 2.3,
        "close_time": datetime.now(timezone.utc).isoformat(),
    })

    with _temp_conn(db) as conn:
        row = conn.execute(
            "SELECT status, r_multiple, trade_ticket FROM trades WHERE trade_id=?",
            ("AI_fb_db_001",),
        ).fetchone()
    assert row["status"] == "CLOSED_WIN"
    assert abs(float(row["r_multiple"]) - 2.3) < 0.01
    assert int(row["trade_ticket"]) == 99001


def test_rejected_execution_feedback_normalizes_status_and_reason(tmp_path, monkeypatch) -> None:
    db = _patch_db(tmp_path, monkeypatch)
    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()

    sig = TechnicalSignal(
        trade_id="AI_fb_reject_001",
        symbol="EURUSD", direction="BUY",
        stop_pips=10.0, take_profit_pips=22.0, risk_reward=2.2,
        confidence=0.65, reason_code="TECH_PULLBACK_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(
        sig, status="PENDING", reason_code="ROUTED_TO_MT5",
        risk_percent=0.032, market_regime="TRENDING_BULL",
    )
    db_mod.update_trade_execution_result({
        "trade_id": "AI_fb_reject_001", "ticket": 0, "status": "REJECTED_LOT",
        "entry_price": 0.0, "slippage": 0.0, "spread_at_entry": 0.00012,
        "profit_loss": 0.0, "r_multiple": 0.0,
        "close_time": datetime.now(timezone.utc).isoformat(),
    })

    with _temp_conn(db) as conn:
        row = conn.execute(
            "SELECT status, reason_code, trade_ticket FROM trades WHERE trade_id=?",
            ("AI_fb_reject_001",),
        ).fetchone()
    assert row["status"] == "REJECTED"
    assert row["reason_code"] == "REJECTED_LOT"
    assert row["trade_ticket"] is None
