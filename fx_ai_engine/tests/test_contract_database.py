"""Contract tests for ``database.db`` written before the Phase 4 split (stage C).

``database.db`` stays the public import path after the split. These tests pin
its names and signatures and run the main persistence round trips against a
temporary SQLite file. The fixture points every connection module that exists
at the temporary file, so the same test body runs before and after the split.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from core.account_status import AccountStatus
from core.evidence import EvidenceContext
from core.types import TechnicalSignal

EVIDENCE = EvidenceContext("runtime_mt5_core_srs", "core_srs", "mt5", "mt5:Contract:1")

# Public functions of database.db and their signatures on the unsplit module.
PUBLIC_SIGNATURES = {
    'apply_execution_feedback': "(payload: 'dict[str, Any]', *, dedupe_key: 'str', source_file: 'str', evidence_context: 'EvidenceContext') -> 'str'",
    'archive_legacy_partition_rows': "(*, archive_reason: 'str' = 'manual_archive') -> 'dict[str, int]'",
    'count_legacy_partition_rows': "() -> 'dict[str, int]'",
    'feedback_receipt_exists': "(feedback_kind: 'str', dedupe_key: 'str', *, evidence_context: 'EvidenceContext | None' = None) -> 'bool'",
    'get_conn': "(db_path: 'Path | None' = None) -> 'Iterator[sqlite3.Connection]'",
    'get_engine_runtime_state': "(key: 'str', *, evidence_context: 'EvidenceContext') -> 'str | None'",
    'get_latest_account_metric': "(*, evidence_stream: 'str | None' = None, account_scope: 'str | None' = None) -> 'dict[str, Any] | None'",
    'get_open_trade_ledger': "(*, evidence_stream: 'str | None' = None, account_scope: 'str | None' = None) -> 'dict[str, Any]'",
    'get_recent_r_multiples': "(limit: 'int' = 100) -> 'list[float]'",
    'get_trade_by_identifiers': "(*, trade_id: 'str | None' = None, position_ticket: 'int | None' = None, trade_ticket: 'int | None' = None, evidence_context: 'EvidenceContext | None' = None) -> 'dict[str, Any] | None'",
    'has_unresolved_execution_feedback': "(evidence_context: 'EvidenceContext') -> 'bool'",
    'initialize_schema': "() -> 'None'",
    'insert_account_metrics': "(account_status: 'AccountStatus', *, evidence_context: 'EvidenceContext | None' = None) -> 'None'",
    'insert_decision_funnel_event': "(*, decision_time: 'datetime', stage: 'str', outcome: 'str', reason_code: 'str', symbol: 'str | None' = None, details: 'str' = '', trade_id: 'str | None' = None, evidence_context: 'EvidenceContext | None' = None) -> 'None'",
    'insert_ml_shadow_event': "(*, decision_time: 'datetime', symbol: 'str', stage: 'str', primary_gate_outcome: 'str', shadow_outcome: 'str', reason_code: 'str', details: 'str' = '', trade_id: 'str | None' = None, probability: 'float | None' = None, threshold: 'float | None' = None, model_loaded: 'bool' = False, checkpoint_path: 'str | None' = None, feature_schema_version: 'str | None' = None, evidence_context: 'EvidenceContext | None' = None) -> 'None'",
    'insert_risk_event': "(rule_name: 'str', severity: 'str', reason: 'str', trade_id: 'str | None' = None, *, evidence_context: 'EvidenceContext | None' = None) -> 'None'",
    'insert_trade_proposal': "(signal: 'TechnicalSignal', status: 'str', reason_code: 'str', risk_percent: 'float', market_regime: 'str', *, evidence_context: 'EvidenceContext | None' = None, regime_confidence: 'float' = 0.0, atr_ratio: 'float' = 1.0, is_london_session: 'int' = 0, is_newyork_session: 'int' = 0, rate_differential: 'float' = 0.0) -> 'None'",
    'list_open_trades': "(*, evidence_stream: 'str | None' = None, account_scope: 'str | None' = None) -> 'list[dict[str, Any]]'",
    'list_unresolved_execution_feedback_conflicts': "() -> 'list[dict[str, Any]]'",
    'mark_trade_execution_uncertain': "(trade_id: 'str', reason_code: 'str' = 'PRESERVE_10_EXECUTION_UNCERTAIN') -> 'None'",
    'mark_trade_expired': "(trade_id: 'str', reason_code: 'str' = 'ROUTER_PENDING_EXPIRED') -> 'None'",
    'migrate_add_decision_funnel_events': "() -> 'None'",
    'migrate_add_engine_runtime_state': "() -> 'None'",
    'migrate_add_evidence_partition_columns': "() -> 'None'",
    'migrate_add_feedback_conflicts': "() -> 'None'",
    'migrate_add_feedback_receipts': "() -> 'None'",
    'migrate_add_ml_feature_columns': "() -> 'None'",
    'migrate_add_ml_shadow_events': "() -> 'None'",
    'migrate_add_restart_state_columns': "() -> 'None'",
    'migrate_add_risk_events': "() -> 'None'",
    'migrate_add_trade_lifecycle_events': "() -> 'None'",
    'migrate_add_trade_reservations': "() -> 'None'",
    'migrate_mark_stale_trades': "() -> 'None'",
    'migrate_phase8_columns': "() -> 'None'",
    'record_feedback_receipt': "(feedback_kind: 'str', dedupe_key: 'str', *, trade_id: 'str | None' = None, ticket: 'int | None' = None, status: 'str | None' = None, close_time: 'str | None' = None, source_file: 'str | None' = None, evidence_context: 'EvidenceContext | None' = None) -> 'bool'",
    'record_trade_lifecycle_event': "(trade_id: 'str', event_type: 'str', status: 'str', reason_code: 'str', payload: 'dict[str, Any] | None' = None, *, conn: 'sqlite3.Connection | None' = None) -> 'None'",
    'resolve_execution_feedback_conflict': "(conflict_id: 'int', *, operator_note: 'str', now_utc: 'str | None' = None) -> 'bool'",
    'run_all_migrations': "() -> 'None'",
    'set_engine_runtime_state': "(key: 'str', value: 'str', *, evidence_context: 'EvidenceContext') -> 'None'",
    'trade_exists': "(trade_id: 'str') -> 'bool'",
    'transition_trade_reservation': "(reservation_id: 'str', state: 'str', reason_code: 'str') -> 'bool'",
    'transition_trade_reservation_by_trade_id': "(trade_id: 'str', state: 'str', reason_code: 'str') -> 'bool'",
    'try_create_trade_reservation': "(*, reservation_id: 'str', trade_id: 'str', symbol: 'str', risk_percent: 'float', policy_hash: 'str', expires_at: 'str', max_positions: 'int', max_combined_exposure: 'float', broker_open_positions: 'int', broker_open_risk: 'float') -> 'dict[str, Any]'",
    'update_trade_execution_result': "(payload: 'dict[str, Any]') -> 'bool'",
    'update_trade_exit_result': "(payload: 'dict[str, Any]', *, evidence_context: 'EvidenceContext | None' = None) -> 'bool'",
}

EXPECTED_TABLES = {
    "account_metrics",
    "decision_funnel_events",
    "engine_runtime_state",
    "execution_feedback_conflicts",
    "feedback_receipts",
    "ml_shadow_events",
    "risk_events",
    "trade_lifecycle_events",
    "trade_reservations",
    "trades",
}

CONNECTION_MODULES = ("database.db", "database.connection")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db = importlib.import_module("database.db")
    db_path = tmp_path / "contract.db"
    schema_path = tmp_path / "schema.sql"
    shutil.copyfile(db.SCHEMA_PATH, schema_path)
    for name in CONNECTION_MODULES:
        if importlib.util.find_spec(name) is None:
            continue
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "DB_PATH", db_path)
        monkeypatch.setattr(module, "SCHEMA_PATH", schema_path)
    db.run_all_migrations()
    return db


def _signal(trade_id: str = "T_CONTRACT") -> TechnicalSignal:
    return TechnicalSignal(
        trade_id=trade_id, symbol="EURUSD", direction="BUY", stop_pips=10.0, take_profit_pips=22.0,
        risk_reward=2.2, confidence=0.7, reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc="2026-11-03T16:30:00+00:00",
    )


def _reserve(db, trade_id: str = "T_CONTRACT", reservation_id: str = "R_CONTRACT") -> dict:
    return db.try_create_trade_reservation(
        reservation_id=reservation_id, trade_id=trade_id, symbol="EURUSD", risk_percent=0.032,
        policy_hash="contract", expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        max_positions=2, max_combined_exposure=0.05, broker_open_positions=0, broker_open_risk=0.0,
    )


def _execution(trade_id: str = "T_CONTRACT") -> dict:
    return {"trade_id": trade_id, "ticket": 123, "position_ticket": 456, "status": "EXECUTED",
            "entry_price": 1.1, "lot_size": 0.5, "slippage": 0.0, "spread_at_entry": 0.0001,
            "profit_loss": 0.0, "r_multiple": 0.0, "close_time": "2026-11-03T16:30:05Z"}


def _reservation_state(db, trade_id: str = "T_CONTRACT") -> str:
    with db.get_conn() as conn:
        return conn.execute("SELECT state FROM trade_reservations WHERE trade_id=?", (trade_id,)).fetchone()[0]


def test_public_names_and_signatures_are_kept():
    db = importlib.import_module("database.db")
    for name in ("DB_PATH", "SCHEMA_PATH"):
        assert hasattr(db, name), name
    actual = {name: str(inspect.signature(getattr(db, name))) for name in PUBLIC_SIGNATURES if hasattr(db, name)}
    assert actual == PUBLIC_SIGNATURES


def test_migrations_are_idempotent_and_create_every_table(temp_db):
    temp_db.run_all_migrations()
    with temp_db.get_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= tables


def test_reservation_lifecycle_round_trip(temp_db):
    assert _reserve(temp_db)["approved"] is True
    assert _reserve(temp_db)["reason_code"] == "RESERVATION_DUPLICATE"
    assert temp_db.transition_trade_reservation("R_CONTRACT", "ROUTED", "ROUTED_TO_MT5")
    assert temp_db.transition_trade_reservation_by_trade_id("T_CONTRACT", "EXECUTED", "EXECUTED")
    assert not temp_db.transition_trade_reservation("R_CONTRACT", "ROUTED", "late")
    assert temp_db.transition_trade_reservation_by_trade_id("T_CONTRACT", "CLOSED", "CLOSED_WIN")
    assert _reservation_state(temp_db) == "CLOSED"
    with pytest.raises(ValueError):
        temp_db.transition_trade_reservation("R_CONTRACT", "BOGUS", "x")


def test_proposal_execution_exit_round_trip(temp_db):
    temp_db.insert_trade_proposal(_signal(), "PENDING", "ROUTED_TO_MT5", 0.032, "TRENDING_BULL",
                                  evidence_context=EVIDENCE)
    assert temp_db.trade_exists("T_CONTRACT")
    assert _reserve(temp_db)["approved"] is True
    assert temp_db.transition_trade_reservation("R_CONTRACT", "ROUTED", "ROUTED_TO_MT5")

    payload = _execution()
    assert temp_db.apply_execution_feedback(payload, dedupe_key="k1", source_file="execution_T.json",
                                            evidence_context=EVIDENCE) == "applied"
    assert temp_db.apply_execution_feedback(payload, dedupe_key="k1", source_file="execution_T.json",
                                            evidence_context=EVIDENCE) == "duplicate"
    open_trades = temp_db.list_open_trades(evidence_stream=EVIDENCE.evidence_stream,
                                           account_scope=EVIDENCE.account_scope)
    assert [(t["trade_id"], t["status"], t["position_ticket"]) for t in open_trades] == [
        ("T_CONTRACT", "EXECUTED_OPEN", 456)]
    ledger = temp_db.get_open_trade_ledger(evidence_stream=EVIDENCE.evidence_stream,
                                           account_scope=EVIDENCE.account_scope)
    assert ledger["open_trade_count"] == 1 and ledger["open_position_tickets"] == [456]
    assert _reservation_state(temp_db) == "EXECUTED"

    exit_payload = {"trade_id": "T_CONTRACT", "ticket": 123, "position_ticket": 456, "status": "CLOSED",
                    "profit_loss": 40.0, "r_multiple": 0.8, "close_time": "2026-11-03T18:00:00+00:00"}
    assert temp_db.update_trade_exit_result(exit_payload, evidence_context=EVIDENCE)
    assert temp_db.transition_trade_reservation_by_trade_id("T_CONTRACT", "CLOSED", "CLOSED")
    assert temp_db.list_open_trades(evidence_stream=EVIDENCE.evidence_stream,
                                    account_scope=EVIDENCE.account_scope) == []
    row = temp_db.get_trade_by_identifiers(position_ticket=456, evidence_context=EVIDENCE)
    assert (row["trade_id"], row["status"], row["r_multiple"]) == ("T_CONTRACT", "CLOSED", 0.8)
    assert temp_db.get_recent_r_multiples() == [0.8]
    assert not temp_db.has_unresolved_execution_feedback(EVIDENCE)
    with temp_db.get_conn() as conn:
        events = [r[0] for r in conn.execute(
            "SELECT event_type FROM trade_lifecycle_events WHERE trade_id='T_CONTRACT' ORDER BY id")]
    assert events == ["PROPOSAL", "EXECUTION", "EXIT"]


def test_uncertain_and_expired_trades(temp_db):
    temp_db.insert_trade_proposal(_signal("T_UNCERTAIN"), "PENDING", "ROUTED_TO_MT5", 0.032, "TRENDING_BULL",
                                  evidence_context=EVIDENCE)
    assert _reserve(temp_db, "T_UNCERTAIN", "R_UNCERTAIN")["approved"] is True
    temp_db.mark_trade_execution_uncertain("T_UNCERTAIN", "ROUTER_PENDING_UNCERTAIN")
    assert _reservation_state(temp_db, "T_UNCERTAIN") == "EXECUTION_UNCERTAIN"
    assert temp_db.has_unresolved_execution_feedback(EVIDENCE)

    temp_db.insert_trade_proposal(_signal("T_EXPIRED"), "PENDING", "ROUTED_TO_MT5", 0.032, "TRENDING_BULL",
                                  evidence_context=EVIDENCE)
    temp_db.mark_trade_expired("T_EXPIRED")
    assert temp_db.get_trade_by_identifiers(trade_id="T_EXPIRED")["status"] == "EXPIRED"


def test_feedback_receipts_and_conflicts(temp_db):
    assert not temp_db.feedback_receipt_exists("exit", "k-exit", evidence_context=EVIDENCE)
    assert temp_db.record_feedback_receipt("exit", "k-exit", trade_id="T", ticket=7, status="CLOSED",
                                           close_time="2026-11-03T18:00:00+00:00", source_file="exit_7.json",
                                           evidence_context=EVIDENCE) is True
    assert temp_db.record_feedback_receipt("exit", "k-exit", evidence_context=EVIDENCE) is False
    assert temp_db.feedback_receipt_exists("exit", "k-exit", evidence_context=EVIDENCE)

    assert temp_db.apply_execution_feedback(_execution("T_MISSING"), dedupe_key="k-missing",
                                            source_file="execution_M.json", evidence_context=EVIDENCE) == "unmatched"
    conflicts = temp_db.list_unresolved_execution_feedback_conflicts()
    assert [(c["trade_id"], c["reason"]) for c in conflicts] == [("T_MISSING", "TRADE_NOT_FOUND_IN_EVIDENCE_SCOPE")]
    assert temp_db.resolve_execution_feedback_conflict(
        conflicts[0]["id"], operator_note="contract test: no order or deal exists in MT5 history")
    assert temp_db.list_unresolved_execution_feedback_conflicts() == []


def test_risk_funnel_shadow_and_account_rows_are_readable(temp_db):
    now = datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc)
    temp_db.insert_risk_event("HARD_RISK", "BLOCK", "contract", "T1", evidence_context=EVIDENCE)
    temp_db.insert_decision_funnel_event(decision_time=now, stage="REGIME", outcome="PASS",
                                         reason_code="REGIME_TREND_BULL", symbol="EURUSD",
                                         evidence_context=EVIDENCE)
    temp_db.insert_ml_shadow_event(decision_time=now, symbol="EURUSD", stage="POST_HARD_RISK",
                                   primary_gate_outcome="ROUTE", shadow_outcome="KEEP", reason_code="SHADOW",
                                   probability=0.6, threshold=0.5, model_loaded=True, evidence_context=EVIDENCE)
    temp_db.insert_account_metrics(AccountStatus(balance=100000.0, equity=100050.0, consecutive_losses=2,
                                                 updated_at=now), evidence_context=EVIDENCE)
    with temp_db.get_conn() as conn:
        assert conn.execute("SELECT rule_name, severity, account_scope FROM risk_events").fetchone()[:] == (
            "HARD_RISK", "BLOCK", EVIDENCE.account_scope)
        assert conn.execute("SELECT stage, outcome, reason_code FROM decision_funnel_events").fetchone()[:] == (
            "REGIME", "PASS", "REGIME_TREND_BULL")
        assert conn.execute("SELECT shadow_outcome, probability, model_loaded FROM ml_shadow_events").fetchone()[:] == (
            "KEEP", 0.6, 1)
    metric = temp_db.get_latest_account_metric(evidence_stream=EVIDENCE.evidence_stream,
                                               account_scope=EVIDENCE.account_scope)
    assert (metric["balance"], metric["equity"], metric["consecutive_losses"]) == (100000.0, 100050.0, 2)
    assert temp_db.count_legacy_partition_rows()["total"] == 0


def test_engine_runtime_state_round_trip(temp_db):
    other = EvidenceContext("runtime_mt5_core_srs", "core_srs", "mt5", "mt5:Contract:2")
    assert temp_db.get_engine_runtime_state("k", evidence_context=EVIDENCE) is None
    temp_db.set_engine_runtime_state("k", "v1", evidence_context=EVIDENCE)
    temp_db.set_engine_runtime_state("k", "v2", evidence_context=EVIDENCE)
    assert temp_db.get_engine_runtime_state("k", evidence_context=EVIDENCE) == "v2"
    assert temp_db.get_engine_runtime_state("k", evidence_context=other) is None
