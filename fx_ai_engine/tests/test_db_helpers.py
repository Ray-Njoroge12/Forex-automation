from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.account_status import AccountStatus
from core.evidence import EvidenceContext
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
    db_mod.migrate_add_decision_funnel_events()
    db_mod.migrate_add_ml_feature_columns()
    db_mod.migrate_add_restart_state_columns()
    db_mod.migrate_add_evidence_partition_columns()

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
        rsi_slope=3.5,
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
            "position_ticket": 777001,
            "status": "EXECUTED",
            "entry_price": 1.10123,
            "slippage": 0.00002,
            "spread_at_entry": 0.00011,
            "profit_loss": 0.0,
            "r_multiple": 0.0,
            "close_time": datetime.now(timezone.utc).isoformat(),
        }
    )
    db_mod.update_trade_exit_result(
        {
            "ticket": 777001,
            "position_ticket": 777001,
            "trade_id": signal.trade_id,
            "status": "CLOSED_WIN",
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
    db_mod.insert_decision_funnel_event(
        decision_time=datetime.now(timezone.utc),
        stage="ROUTER",
        outcome="ROUTED",
        reason_code="ROUTED_TO_MT5",
        symbol=signal.symbol,
        trade_id=signal.trade_id,
    )

    with _temp_conn(temp_db) as conn:
        trade = conn.execute(
            "SELECT trade_ticket, position_ticket, status, r_multiple, rsi_slope FROM trades WHERE trade_id=?",
            (signal.trade_id,),
        ).fetchone()
        metrics = conn.execute("SELECT COUNT(*) AS n FROM account_metrics").fetchone()
        events = conn.execute("SELECT COUNT(*) AS n FROM risk_events").fetchone()
        funnel = conn.execute("SELECT COUNT(*) AS n FROM decision_funnel_events").fetchone()

    assert trade["trade_ticket"] == 999001
    assert trade["position_ticket"] == 777001
    assert trade["status"] == "CLOSED_WIN"
    assert float(trade["r_multiple"]) == 2.4
    assert float(trade["rsi_slope"]) == 3.5
    assert metrics["n"] == 1
    assert events["n"] == 1
    assert funnel["n"] == 1


def test_decision_funnel_event_respects_evidence_context(tmp_path, monkeypatch) -> None:
    temp_db = tmp_path / "trading_state.db"
    temp_schema = tmp_path / "schema.sql"
    temp_schema.write_text(db_mod.SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(db_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(db_mod, "SCHEMA_PATH", temp_schema)
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))

    db_mod.initialize_schema()
    live_ctx = EvidenceContext("runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1")

    db_mod.insert_decision_funnel_event(
        decision_time=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
        stage="TECHNICAL",
        outcome="REJECT",
        reason_code="TECH_PULLBACK_OR_RSI_INVALID",
        symbol="EURUSD",
        details="pulled_back=False rsi_ok=True",
        evidence_context=live_ctx,
    )

    with _temp_conn(temp_db) as conn:
        row = conn.execute(
            "SELECT evidence_stream, policy_mode, execution_mode, account_scope, stage, outcome, reason_code, symbol FROM decision_funnel_events"
        ).fetchone()

    assert dict(row) == {
        "evidence_stream": "runtime_mt5_core_srs",
        "policy_mode": "core_srs",
        "execution_mode": "mt5",
        "account_scope": "mt5:demo:1",
        "stage": "TECHNICAL",
        "outcome": "REJECT",
        "reason_code": "TECH_PULLBACK_OR_RSI_INVALID",
        "symbol": "EURUSD",
    }


def test_mark_trade_expired_updates_pending_only(tmp_path, monkeypatch) -> None:
    temp_db = tmp_path / "trading_state.db"
    temp_schema = tmp_path / "schema.sql"
    temp_schema.write_text(db_mod.SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(db_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(db_mod, "SCHEMA_PATH", temp_schema)
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))

    db_mod.initialize_schema()
    db_mod.migrate_add_restart_state_columns()
    db_mod.migrate_add_evidence_partition_columns()
    sig = TechnicalSignal(
        trade_id="AI_expire_1",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(sig, "PENDING", "ROUTED_TO_MT5", 0.02, "TRENDING_BULL")
    db_mod.mark_trade_expired(sig.trade_id, "ROUTER_PENDING_EXPIRED")

    with _temp_conn(temp_db) as conn:
        row = conn.execute("SELECT status, reason_code FROM trades WHERE trade_id=?", (sig.trade_id,)).fetchone()
    assert row["status"] == "EXPIRED"
    assert row["reason_code"] == "ROUTER_PENDING_EXPIRED"


def test_execution_uncertain_trade_stays_on_open_ledger(tmp_path, monkeypatch) -> None:
    temp_db = tmp_path / "trading_state.db"
    temp_schema = tmp_path / "schema.sql"
    temp_schema.write_text(db_mod.SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(db_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(db_mod, "SCHEMA_PATH", temp_schema)
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))

    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_evidence_partition_columns()
    sig = TechnicalSignal(
        trade_id="AI_uncertain_1",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(sig, "PENDING", "ROUTED_TO_MT5", 0.02, "TRENDING_BULL")
    db_mod.mark_trade_execution_uncertain(sig.trade_id, "ROUTER_PENDING_UNCERTAIN")

    ledger = db_mod.get_open_trade_ledger()

    assert ledger["open_trade_count"] == 1
    assert ledger["open_symbols"] == ["EURUSD"]
    assert ledger["open_risk_percent"] == 0.02


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
    db_mod.migrate_add_restart_state_columns()
    db_mod.migrate_add_evidence_partition_columns()

    with _temp_conn(temp_db) as conn:
        trades_cols = {row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
        metrics_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(account_metrics)").fetchall()
        }

    assert {"trade_id", "position_ticket", "direction", "risk_percent", "reason_code", "spread_entry", "slippage"} <= trades_cols
    assert {
        "open_risk_percent",
        "open_usd_exposure_count",
        "drawdown_percent",
        "peak_equity",
        "evidence_stream",
        "policy_mode",
        "execution_mode",
        "account_scope",
        "daily_anchor_date",
        "daily_anchor_equity",
        "weekly_anchor_key",
        "weekly_anchor_equity",
    } <= metrics_cols


def test_restart_state_persistence_and_open_trade_ledger(tmp_path, monkeypatch) -> None:
    temp_db = tmp_path / "trading_state.db"
    temp_schema = tmp_path / "schema.sql"
    temp_schema.write_text(db_mod.SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(db_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(db_mod, "SCHEMA_PATH", temp_schema)
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))

    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_restart_state_columns()
    db_mod.migrate_add_evidence_partition_columns()

    sig = TechnicalSignal(
        trade_id="AI_restart_001",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(sig, "PENDING", "ROUTED_TO_MT5", 0.032, "TRENDING_BULL")
    db_mod.update_trade_execution_result(
        {
            "trade_id": sig.trade_id,
            "ticket": 99123,
            "status": "EXECUTED",
            "entry_price": 1.1002,
            "slippage": 0.00001,
            "spread_at_entry": 0.0001,
            "close_time": datetime.now(timezone.utc).isoformat(),
        }
    )

    status = AccountStatus(
        balance=995.0,
        equity=990.0,
        open_risk_percent=0.032,
        daily_loss_percent=0.01,
        weekly_loss_percent=0.01,
        drawdown_percent=0.02,
        peak_equity=1010.0,
        daily_anchor_date="2026-03-06",
        daily_anchor_equity=1000.0,
        weekly_anchor_key="2026-W10",
        weekly_anchor_equity=1005.0,
        consecutive_losses=1,
    )
    db_mod.insert_account_metrics(status)

    latest = db_mod.get_latest_account_metric()
    ledger = db_mod.get_open_trade_ledger()

    assert latest is not None
    assert latest["peak_equity"] == 1010.0
    assert latest["daily_anchor_date"] == "2026-03-06"
    assert latest["weekly_anchor_key"] == "2026-W10"
    assert latest["consecutive_losses"] == 1
    assert ledger["open_trade_count"] == 1
    assert ledger["open_symbols"] == ["EURUSD"]
    assert ledger["open_risk_percent"] == 0.032


def test_evidence_partitioning_scopes_runtime_reads(tmp_path, monkeypatch) -> None:
    temp_db = tmp_path / "trading_state.db"
    temp_schema = tmp_path / "schema.sql"
    temp_schema.write_text(db_mod.SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(db_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(db_mod, "SCHEMA_PATH", temp_schema)
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))

    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_restart_state_columns()
    db_mod.migrate_add_evidence_partition_columns()

    live_ctx = EvidenceContext("runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1")
    mock_ctx = EvidenceContext("runtime_mock_core_srs", "core_srs", "mock", "mock")

    live_sig = TechnicalSignal(
        trade_id="AI_live_001",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    mock_sig = TechnicalSignal(
        trade_id="AI_mock_001",
        symbol="GBPUSD",
        direction="SELL",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_SELL",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )

    db_mod.insert_trade_proposal(live_sig, "EXECUTION_UNCERTAIN", "ROUTED_TO_MT5", 0.032, "TRENDING_BULL", evidence_context=live_ctx)
    db_mod.insert_trade_proposal(mock_sig, "EXECUTION_UNCERTAIN", "ROUTED_TO_MT5", 0.016, "TRENDING_BEAR", evidence_context=mock_ctx)
    db_mod.insert_account_metrics(AccountStatus(balance=1000.0, equity=995.0), evidence_context=live_ctx)
    db_mod.insert_account_metrics(AccountStatus(balance=10000.0, equity=10000.0), evidence_context=mock_ctx)

    latest = db_mod.get_latest_account_metric(
        evidence_stream=live_ctx.evidence_stream,
        account_scope=live_ctx.account_scope,
    )
    ledger = db_mod.get_open_trade_ledger(
        evidence_stream=live_ctx.evidence_stream,
        account_scope=live_ctx.account_scope,
    )

    assert latest is not None
    assert float(latest["equity"]) == 995.0
    assert ledger["open_trade_count"] == 1
    assert ledger["open_symbols"] == ["EURUSD"]


def test_non_final_exit_feedback_does_not_close_trade(tmp_path, monkeypatch) -> None:
    temp_db = tmp_path / "trading_state.db"
    temp_schema = tmp_path / "schema.sql"
    temp_schema.write_text(db_mod.SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(db_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(db_mod, "SCHEMA_PATH", temp_schema)
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))

    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()

    sig = TechnicalSignal(
        trade_id="AI_partial_001",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(sig, "PENDING", "ROUTED_TO_MT5", 0.032, "TRENDING_BULL")
    db_mod.update_trade_execution_result(
        {
            "trade_id": sig.trade_id,
            "ticket": 991001,
            "position_ticket": 881001,
            "status": "EXECUTED",
            "entry_price": 1.10123,
            "slippage": 0.00002,
            "spread_at_entry": 0.00011,
            "profit_loss": 0.0,
            "r_multiple": 0.0,
            "close_time": datetime.now(timezone.utc).isoformat(),
        }
    )

    db_mod.update_trade_exit_result(
        {
            "ticket": 881001,
            "position_ticket": 881001,
            "trade_id": sig.trade_id,
            "status": "CLOSED_WIN",
            "profit_loss": 6.1,
            "r_multiple": 0.8,
            "is_final_exit": False,
            "close_time": datetime.now(timezone.utc).isoformat(),
        }
    )

    with _temp_conn(temp_db) as conn:
        row = conn.execute(
            "SELECT status, profit_loss, r_multiple FROM trades WHERE trade_id=?",
            (sig.trade_id,),
        ).fetchone()

    assert row["status"] == "EXECUTED_OPEN"
    assert float(row["profit_loss"] or 0.0) == 0.0
    assert float(row["r_multiple"] or 0.0) == 0.0


def test_exit_update_is_scoped_to_evidence_context(tmp_path, monkeypatch) -> None:
    temp_db = tmp_path / "trading_state.db"
    temp_schema = tmp_path / "schema.sql"
    temp_schema.write_text(db_mod.SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(db_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(db_mod, "SCHEMA_PATH", temp_schema)
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))

    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_evidence_partition_columns()

    live_ctx = EvidenceContext("runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1")
    other_ctx = EvidenceContext("runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:2")

    for trade_id, ctx in (("AI_scope_live", live_ctx), ("AI_scope_other", other_ctx)):
        sig = TechnicalSignal(
            trade_id=trade_id,
            symbol="EURUSD",
            direction="BUY",
            stop_pips=10.0,
            take_profit_pips=22.0,
            risk_reward=2.2,
            confidence=0.7,
            reason_code="TECH_CONFIRMED_BUY",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )
        db_mod.insert_trade_proposal(sig, "PENDING", "ROUTED_TO_MT5", 0.032, "TRENDING_BULL", evidence_context=ctx)
        db_mod.update_trade_execution_result(
            {
                "trade_id": trade_id,
                "ticket": 900001 if trade_id.endswith("live") else 900002,
                "position_ticket": 880001,
                "status": "EXECUTED",
                "entry_price": 1.10123,
                "slippage": 0.00002,
                "spread_at_entry": 0.00011,
                "profit_loss": 0.0,
                "r_multiple": 0.0,
                "close_time": datetime.now(timezone.utc).isoformat(),
            }
        )

    matched = db_mod.update_trade_exit_result(
        {
            "ticket": 880001,
            "position_ticket": 880001,
            "trade_id": "AI_scope_live",
            "status": "CLOSED_WIN",
            "profit_loss": 12.0,
            "r_multiple": 2.0,
            "close_time": datetime.now(timezone.utc).isoformat(),
        },
        evidence_context=live_ctx,
    )

    with _temp_conn(temp_db) as conn:
        rows = conn.execute(
            "SELECT trade_id, status FROM trades WHERE position_ticket=880001 ORDER BY trade_id ASC"
        ).fetchall()

    assert matched is True
    assert [tuple(row) for row in rows] == [
        ("AI_scope_live", "CLOSED_WIN"),
        ("AI_scope_other", "EXECUTED_OPEN"),
    ]
