from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.account_status import AccountStatus
from core.types import TechnicalSignal
from database import db as db_mod


@contextmanager
def _temp_conn(temp_db: Path):
    import sqlite3

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def test_db_schema_and_trade_lifecycle(tmp_path, monkeypatch) -> None:
    temp_db = tmp_path / "trading_state.db"
    temp_schema = tmp_path / "schema.sql"
    temp_schema.write_text(db_mod.SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(db_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(db_mod, "SCHEMA_PATH", temp_schema)
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))

    db_mod.initialize_schema()
    db_mod.migrate_add_risk_events()
    db_mod.migrate_add_ml_feature_columns()

    signal = TechnicalSignal(
        trade_id="AI_20260225_130000_ff22aa",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=11.0,
        take_profit_pips=24.2,
        risk_reward=2.2,
        confidence=0.74,
        reason_code="TECH_PULLBACK_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )

    db_mod.insert_trade_proposal(
        signal,
        status="PENDING",
        reason_code="ROUTED_TO_MT5",
        risk_percent=0.032,
        market_regime="TRENDING_BULL",
    )

    db_mod.update_trade_execution_result(
        {
            "trade_id": signal.trade_id,
            "ticket": 999001,
            "status": "EXECUTED",
            "entry_price": 1.10123,
            "slippage": 0.00002,
            "spread_at_entry": 0.00011,
            "profit_loss": 12.3,
            "r_multiple": 2.4,
            "close_time": datetime.now(timezone.utc).isoformat(),
        }
    )

    status = AccountStatus(
        balance=1012.3,
        equity=1012.3,
        open_risk_percent=0.0,
        open_usd_exposure_count=0,
        daily_loss_percent=0.0,
        weekly_loss_percent=0.0,
        drawdown_percent=0.0,
        consecutive_losses=0,
        is_trading_halted=False,
    )
    db_mod.insert_account_metrics(status)
    db_mod.insert_risk_event("HARD_RISK", "BLOCK", "RISK_DAILY_STOP", signal.trade_id)

    with _temp_conn(temp_db) as conn:
        trade = conn.execute("SELECT trade_ticket, status, r_multiple FROM trades WHERE trade_id=?", (signal.trade_id,)).fetchone()
        metrics = conn.execute("SELECT COUNT(*) AS n FROM account_metrics").fetchone()
        events = conn.execute("SELECT COUNT(*) AS n FROM risk_events").fetchone()

    assert trade["trade_ticket"] == 999001
    assert trade["status"] == "EXECUTED"
    assert float(trade["r_multiple"]) == 2.4
    assert metrics["n"] == 1
    assert events["n"] == 1


def test_phase8_column_migration_from_phase1_baseline(tmp_path, monkeypatch) -> None:
    temp_db = tmp_path / "legacy.db"

    with _temp_conn(temp_db) as conn:
        conn.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_ticket INTEGER UNIQUE,
                symbol TEXT NOT NULL,
                order_type TEXT NOT NULL,
                lot_size REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE account_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                balance REAL NOT NULL,
                equity REAL NOT NULL
            )
            """
        )

    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))
    db_mod.migrate_phase8_columns()

    with _temp_conn(temp_db) as conn:
        trades_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
        metrics_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(account_metrics)").fetchall()
        }

    assert {"trade_id", "direction", "risk_percent", "reason_code", "spread_entry", "slippage"} <= trades_cols
    assert {"open_risk_percent", "open_usd_exposure_count", "drawdown_percent"} <= metrics_cols
