from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from copy import deepcopy
import json

import pandas as pd

from bridge.signal_router import RouterCleanupResult
import database.db as db_mod
import main as main_mod
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


class _FakeSpan:
    def set_attribute(self, _name: str, _value) -> None:
        return None


class _FakeTracer:
    def start_as_current_span(self, _name: str):
        return nullcontext(_FakeSpan())


class _FakeMetrics:
    def inc(self, _name: str) -> None:
        return None

    def set_gauge(self, _name: str, _value: float) -> None:
        return None


class _FakeRanker:
    def load(self) -> bool:
        return False


class _FakeBridge:
    def __init__(self):
        self.open_positions_count = 0
        self.open_symbols = []

    def fetch_ohlc_data(self, *_args, **_kwargs):
        return pd.DataFrame()

    def get_live_spread(self, _symbol: str) -> float:
        return 0.0001

    def get_account_snapshot(self):
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "balance": 1000.0,
            "equity": 1000.0,
            "margin_free": 900.0,
            "open_positions_count": self.open_positions_count,
            "open_symbols": list(self.open_symbols),
            "floating_pnl": 0.0,
        }

    def get_open_position_tickets(self):
        return []

    def get_position_history_summary(self, position_ticket: int):
        if position_ticket != 881001:
            if position_ticket != 881003:
                return None
            return {
                "trade_id": "AI_recover_partial_001",
                "trade_ticket": 991003,
                "position_ticket": 881003,
                "symbol": "EURUSD",
                "direction": "BUY",
                "entry_price": 1.1000,
                "lot_size": 1.0,
                "execution_time": "2026-03-09T12:30:01+00:00",
                "close_price": 1.1010,
                "close_time": "2026-03-09T12:45:00+00:00",
                "profit_loss": 30.0,
                "status": "CLOSED_WIN",
                "close_legs": [
                    {"price": 1.1020, "volume": 0.5, "profit_loss": 20.0, "close_time": "2026-03-09T12:40:00+00:00"},
                    {"price": 1.1010, "volume": 0.5, "profit_loss": 10.0, "close_time": "2026-03-09T12:45:00+00:00"},
                ],
            }
        return {
            "trade_id": "AI_recover_001",
            "trade_ticket": 991001,
            "position_ticket": 881001,
            "symbol": "EURUSD",
            "direction": "BUY",
            "entry_price": 1.1000,
            "lot_size": 0.23,
            "execution_time": "2026-03-09T12:00:01+00:00",
            "close_price": 1.0989,
            "close_time": "2026-03-09T12:15:00+00:00",
            "profit_loss": -12.5,
            "status": "CLOSED_LOSS",
        }

    def get_trade_history_summary(self, trade_id: str):
        if trade_id != "AI_recover_uncertain_001":
            return None
        return {
            "trade_id": trade_id,
            "trade_ticket": 991002,
            "position_ticket": 881002,
            "symbol": "EURUSD",
            "direction": "BUY",
            "entry_price": 1.1010,
            "lot_size": 0.17,
            "execution_time": "2026-03-09T12:05:01+00:00",
            "close_price": 1.1032,
            "close_time": "2026-03-09T12:20:00+00:00",
            "profit_loss": 15.0,
            "status": "CLOSED_WIN",
        }


class _FallbackHistoryBridge(_FakeBridge):
    def __init__(self):
        super().__init__()
        self.position_history_calls: list[int] = []
        self.trade_history_calls: list[str] = []

    def get_position_history_summary(self, position_ticket: int):
        self.position_history_calls.append(position_ticket)
        return None

    def get_trade_history_summary(self, trade_id: str):
        self.trade_history_calls.append(trade_id)
        if trade_id != "AI_recover_placeholder_001":
            return None
        return {
            "trade_id": trade_id,
            "trade_ticket": 991123,
            "position_ticket": 881123,
            "symbol": "EURUSD",
            "direction": "BUY",
            "entry_price": 1.1010,
            "lot_size": 0.23,
            "execution_time": "2026-03-09T12:05:01+00:00",
            "close_price": 1.1032,
            "close_time": "2026-03-09T12:20:00+00:00",
            "profit_loss": 15.0,
            "status": "CLOSED_WIN",
        }


def _build_engine(monkeypatch, tmp_path: Path, *, bridge=None, policy=None):
    temp_db = tmp_path / "trading_state.db"
    temp_schema = tmp_path / "schema.sql"
    temp_schema.write_text(db_mod.SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(db_mod, "DB_PATH", temp_db)
    monkeypatch.setattr(db_mod, "SCHEMA_PATH", temp_schema)
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=temp_db: _temp_conn(temp_db))
    monkeypatch.setattr(main_mod, "get_mt5_bridge_path", lambda: tmp_path)
    monkeypatch.setattr(main_mod, "load_calendar", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main_mod, "load_rate_differentials", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main_mod, "SentimentAgent", lambda: object())
    monkeypatch.setattr(main_mod, "SignalRanker", _FakeRanker)
    for folder in ("pending_signals", "feedback", "exits", "active_locks"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    db_mod.initialize_schema()
    db_mod.migrate_phase8_columns()
    db_mod.migrate_add_evidence_partition_columns()
    return main_mod.Engine(
        bridge=bridge or _FakeBridge(),
        tracer=_FakeTracer(),
        metrics=_FakeMetrics(),
        use_mock=True,
        run_mode="smoke",
        policy=policy,
    )


def test_engine_recovers_broker_closed_trade_before_state_sync(tmp_path, monkeypatch) -> None:
    engine = _build_engine(monkeypatch, tmp_path)
    sig = TechnicalSignal(
        trade_id="AI_recover_001",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(
        sig,
        status="PENDING",
        reason_code="ROUTED_TO_MT5",
        risk_percent=0.032,
        market_regime="TRENDING_BULL",
        evidence_context=engine.evidence_context,
    )
    db_mod.update_trade_execution_result({"trade_id": sig.trade_id, "ticket": 991001, "position_ticket": 881001, "status": "EXECUTED"})

    engine._recover_missing_trade_closures_from_broker()
    engine._update_account_state()

    with _temp_conn(db_mod.DB_PATH) as conn:
        row = conn.execute("SELECT status, lot_size, entry_price, profit_loss, r_multiple FROM trades WHERE trade_id=?", (sig.trade_id,)).fetchone()
    assert row["status"] == "CLOSED_LOSS"
    assert float(row["lot_size"]) == 0.23
    assert float(row["entry_price"]) == 1.1
    assert float(row["profit_loss"]) == -12.5
    assert float(row["r_multiple"]) == -1.1
    assert engine.account_status.consecutive_losses == 1
    assert engine.account_status.state_reconciled is True
    assert engine.account_status.is_trading_halted is False


def test_update_account_state_preserves_dirty_consecutive_losses(tmp_path, monkeypatch) -> None:
    engine = _build_engine(monkeypatch, tmp_path)
    engine.account_status.consecutive_losses = 1
    db_mod.insert_account_metrics(engine.account_status, evidence_context=engine.evidence_context)

    engine.account_status.consecutive_losses = 2
    engine._consecutive_losses_dirty = True

    engine._update_account_state()

    assert engine.account_status.consecutive_losses == 2


def test_engine_recovers_partial_close_trade_with_weighted_r_multiple(tmp_path, monkeypatch) -> None:
    engine = _build_engine(monkeypatch, tmp_path)
    sig = TechnicalSignal(
        trade_id="AI_recover_partial_001",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(
        sig,
        status="PENDING",
        reason_code="ROUTED_TO_MT5",
        risk_percent=0.032,
        market_regime="TRENDING_BULL",
        evidence_context=engine.evidence_context,
    )
    db_mod.update_trade_execution_result(
        {"trade_id": sig.trade_id, "ticket": 991003, "position_ticket": 881003, "status": "EXECUTED"}
    )

    engine._recover_missing_trade_closures_from_broker()

    with _temp_conn(db_mod.DB_PATH) as conn:
        row = conn.execute(
            "SELECT status, profit_loss, r_multiple FROM trades WHERE trade_id=?",
            (sig.trade_id,),
        ).fetchone()
    assert row["status"] == "CLOSED_WIN"
    assert float(row["profit_loss"]) == 30.0
    assert float(row["r_multiple"]) == 1.5
    assert engine.account_status.consecutive_losses == 0


def test_restart_loss_update_seeds_persisted_consecutive_losses_before_first_sync(tmp_path, monkeypatch) -> None:
    first_engine = _build_engine(monkeypatch, tmp_path)
    first_engine.account_status.consecutive_losses = 2
    db_mod.insert_account_metrics(first_engine.account_status, evidence_context=first_engine.evidence_context)

    restarted_engine = _build_engine(monkeypatch, tmp_path)
    restarted_engine._record_consecutive_loss_update(-5.0)
    restarted_engine._update_account_state()

    assert restarted_engine.account_status.consecutive_losses == 3


def test_engine_recovers_uncertain_trade_without_stored_tickets_by_trade_id(tmp_path, monkeypatch) -> None:
    engine = _build_engine(monkeypatch, tmp_path)
    sig = TechnicalSignal(
        trade_id="AI_recover_uncertain_001",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(
        sig,
        status="PENDING",
        reason_code="ROUTED_TO_MT5",
        risk_percent=0.032,
        market_regime="TRENDING_BULL",
        evidence_context=engine.evidence_context,
    )
    db_mod.mark_trade_execution_uncertain(sig.trade_id, "lost execution ack")

    engine._recover_missing_trade_closures_from_broker()

    with _temp_conn(db_mod.DB_PATH) as conn:
        row = conn.execute(
            "SELECT status, trade_ticket, position_ticket, lot_size, entry_price, profit_loss FROM trades WHERE trade_id=?",
            (sig.trade_id,),
        ).fetchone()
    assert row["status"] == "CLOSED_WIN"
    assert int(row["trade_ticket"]) == 991002
    assert int(row["position_ticket"]) == 881002
    assert float(row["lot_size"]) == 0.17
    assert float(row["entry_price"]) == 1.101
    assert float(row["profit_loss"]) == 15.0
    assert engine.account_status.consecutive_losses == 0


def test_update_account_state_does_not_halt_on_stale_snapshot_after_exit_feedback(tmp_path, monkeypatch) -> None:
    engine = _build_engine(monkeypatch, tmp_path)
    sig = TechnicalSignal(
        trade_id="AI_exit_stale_001",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(
        sig,
        status="PENDING",
        reason_code="ROUTED_TO_MT5",
        risk_percent=0.032,
        market_regime="TRENDING_BULL",
        evidence_context=engine.evidence_context,
    )
    db_mod.update_trade_execution_result(
        {"trade_id": sig.trade_id, "ticket": 991011, "position_ticket": 881011, "status": "EXECUTED"}
    )
    (tmp_path / "exits" / "exit_991011.json").write_text(
        json.dumps(
            {
                "trade_id": sig.trade_id,
                "ticket": 991011,
                "position_ticket": 881011,
                "profit_loss": 8.5,
                "status": "CLOSED_WIN",
                "close_time": "2026-03-11T10:05:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "feedback" / "account_snapshot.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-03-11T10:00:00+00:00",
                "balance": 1000.0,
                "equity": 1005.0,
                "margin_free": 900.0,
                "open_positions_count": 1,
                "open_symbols": ["EURUSD"],
                "floating_pnl": 5.0,
            }
        ),
        encoding="utf-8",
    )

    engine._consume_feedback()
    engine._update_account_state()

    with _temp_conn(db_mod.DB_PATH) as conn:
        row = conn.execute(
            "SELECT status, profit_loss FROM trades WHERE trade_id=?",
            (sig.trade_id,),
        ).fetchone()
    assert row["status"] == "CLOSED_WIN"
    assert float(row["profit_loss"]) == 8.5
    assert engine.account_status.state_reconciled is True
    assert engine.account_status.is_trading_halted is False


def test_engine_recovery_falls_back_to_trade_id_when_placeholder_position_ticket_misses(tmp_path, monkeypatch) -> None:
    bridge = _FallbackHistoryBridge()
    engine = _build_engine(monkeypatch, tmp_path, bridge=bridge)
    sig = TechnicalSignal(
        trade_id="AI_recover_placeholder_001",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(
        sig,
        status="PENDING",
        reason_code="ROUTED_TO_MT5",
        risk_percent=0.032,
        market_regime="TRENDING_BULL",
        evidence_context=engine.evidence_context,
    )
    db_mod.update_trade_execution_result(
        {"trade_id": sig.trade_id, "ticket": 991123, "status": "EXECUTED"}
    )

    engine._recover_missing_trade_closures_from_broker()
    engine._update_account_state()

    with _temp_conn(db_mod.DB_PATH) as conn:
        row = conn.execute(
            "SELECT status, trade_ticket, position_ticket, lot_size, entry_price, profit_loss FROM trades WHERE trade_id=?",
            (sig.trade_id,),
        ).fetchone()
    assert bridge.position_history_calls == [991123]
    assert bridge.trade_history_calls == [sig.trade_id]
    assert row["status"] == "CLOSED_WIN"
    assert int(row["trade_ticket"]) == 991123
    assert int(row["position_ticket"]) == 881123
    assert float(row["lot_size"]) == 0.23
    assert float(row["entry_price"]) == 1.101
    assert float(row["profit_loss"]) == 15.0
    assert engine.account_status.state_reconciled is True
    assert engine.account_status.is_trading_halted is False


def test_run_consumes_feedback_before_state_sync(tmp_path, monkeypatch) -> None:
    engine = _build_engine(monkeypatch, tmp_path)
    order: list[str] = []
    monkeypatch.setattr(engine, "_consume_feedback", lambda: order.append("consume"))
    monkeypatch.setattr(engine, "_recover_missing_trade_closures_from_broker", lambda: order.append("recover"))
    monkeypatch.setattr(engine, "_update_account_state", lambda: order.append("update"))
    monkeypatch.setattr(engine, "_is_new_m15_candle", lambda: False)
    monkeypatch.setattr(main_mod, "insert_account_metrics", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda *_args, **_kwargs: None)

    engine.run(mode="smoke")

    assert order[:3] == ["consume", "recover", "update"]


def test_update_account_state_option_c_halts_when_management_restore_health_missing(tmp_path, monkeypatch) -> None:
    bridge = _FakeBridge()
    bridge.open_positions_count = 1
    bridge.open_symbols = ["AUDUSD"]
    policy = deepcopy(main_mod.get_policy_config())
    policy["EXPERIMENTS"]["LIVE_TRADE_MGMT_OPTION_C"]["enabled"] = True
    engine = _build_engine(
        monkeypatch,
        tmp_path,
        bridge=bridge,
        policy=policy,
    )
    sig = TechnicalSignal(
        trade_id="AI_optionc_001",
        symbol="AUDUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(
        sig,
        status="PENDING",
        reason_code="ROUTED_TO_MT5",
        risk_percent=0.032,
        market_regime="TRENDING_BULL",
        evidence_context=engine.evidence_context,
    )
    db_mod.update_trade_execution_result(
        {"trade_id": sig.trade_id, "ticket": 991003, "position_ticket": 1880903, "status": "EXECUTED"}
    )

    engine._update_account_state()

    assert engine.account_status.is_trading_halted is True
    assert engine.account_status.state_reconciled is False
    assert "management restore health unavailable" in engine.account_status.state_reconciliation_reason


def test_update_account_state_option_c_accepts_restored_management_snapshot(tmp_path, monkeypatch) -> None:
    bridge = _FakeBridge()
    bridge.open_positions_count = 1
    bridge.open_symbols = ["AUDUSD"]
    policy = deepcopy(main_mod.get_policy_config())
    policy["EXPERIMENTS"]["LIVE_TRADE_MGMT_OPTION_C"]["enabled"] = True
    engine = _build_engine(
        monkeypatch,
        tmp_path,
        bridge=bridge,
        policy=policy,
    )
    sig = TechnicalSignal(
        trade_id="AI_optionc_002",
        symbol="AUDUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(
        sig,
        status="PENDING",
        reason_code="ROUTED_TO_MT5",
        risk_percent=0.032,
        market_regime="TRENDING_BULL",
        evidence_context=engine.evidence_context,
    )
    db_mod.update_trade_execution_result(
        {"trade_id": sig.trade_id, "ticket": 991004, "position_ticket": 1880904, "status": "EXECUTED"}
    )
    (tmp_path / "feedback" / "account_snapshot.json").write_text(
        '{'
        '"timestamp":"2026-03-11T10:00:00+00:00",'
        '"balance":1000.0,'
        '"equity":998.0,'
        '"margin_free":900.0,'
        '"open_positions_count":1,'
        '"open_symbols":["AUDUSD"],'
        '"floating_pnl":-2.0,'
        '"management_state_restored":true,'
        '"managed_positions_count":1,'
        '"managed_position_tickets":[1880904],'
        '"unmanaged_position_tickets":[]'
        '}',
        encoding="utf-8",
    )

    engine._update_account_state()

    assert engine.account_status.is_trading_halted is False
    assert engine.account_status.state_reconciled is True


def test_non_preserve_router_housekeeping_marks_uncertain_and_halts(tmp_path, monkeypatch) -> None:
    engine = _build_engine(monkeypatch, tmp_path)
    uncertain_marks: list[tuple[str, str]] = []
    risk_events: list[tuple[str, str, str, str | None]] = []
    evaluated: list[str] = []

    monkeypatch.setattr(main_mod, "is_tradeable_session", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        engine.router,
        "cleanup_stale",
        lambda max_age_seconds=600: RouterCleanupResult(
            stale_pending_trade_ids=("AI_stale_001",),
            orphan_lock_trade_ids=("AI_orphan_001",),
        ),
    )
    monkeypatch.setattr(
        main_mod,
        "mark_trade_execution_uncertain",
        lambda trade_id, reason_code, evidence_context=None: uncertain_marks.append((trade_id, reason_code)),
    )
    monkeypatch.setattr(
        engine,
        "_insert_risk_event",
        lambda rule_name, severity, reason, trade_id=None: risk_events.append((rule_name, severity, reason, trade_id)),
    )
    monkeypatch.setattr(engine, "_evaluate_symbol", lambda symbol, *_args, **_kwargs: evaluated.append(symbol))

    engine._decision_cycle()

    assert uncertain_marks == [
        ("AI_stale_001", "ROUTER_PENDING_UNCERTAIN"),
        ("AI_orphan_001", "ROUTER_LOCK_UNCERTAIN"),
    ]
    assert risk_events == [
        (
            "BRIDGE_EXECUTION_UNCERTAIN",
            "BLOCK",
            "stale pending signal quarantined; execution truth is uncertain",
            "AI_stale_001",
        ),
        (
            "BRIDGE_EXECUTION_UNCERTAIN",
            "BLOCK",
            "orphan router lock quarantined; execution truth is uncertain",
            "AI_orphan_001",
        ),
    ]
    assert evaluated == []
    assert engine.account_status.is_trading_halted is True
    assert engine.account_status.state_reconciled is False
    assert "bridge uncertainty detected during router housekeeping" in engine.account_status.state_reconciliation_reason
