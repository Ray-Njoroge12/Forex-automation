from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.evidence import EvidenceContext
from core.account_status import AccountStatus
from core.schemas import SchemaError, validate_execution_feedback
from database import connection as db_connection
from database import db
from main import Engine, _execution_feedback_dedupe_key


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "state.db"

    @contextmanager
    def connection(db_path=None):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(db_connection, "get_conn", connection)
    db.initialize_schema()
    evidence = EvidenceContext("stream", "core", "mt5", "account")
    with connection() as conn:
        conn.execute("INSERT INTO trades (trade_id, symbol, status, evidence_stream, account_scope) VALUES ('T1', 'EURUSD', 'PENDING', 'stream', 'account')")
        conn.execute("""INSERT INTO trade_reservations
            (reservation_id, trade_id, symbol, risk_percent, policy_hash, state, reason_code, created_at, updated_at, expires_at)
            VALUES ('R1','T1','EURUSD',0.02,'hash','ROUTED','test','a','a','z')""")
    return connection, evidence


def payload(status="EXECUTED"):
    return {"trade_id": "T1", "ticket": 123 if status == "EXECUTED" else 0,
            "position_ticket": 456 if status == "EXECUTED" else 0, "status": status,
            "entry_price": 1.1 if status == "EXECUTED" else 0.0,
            "lot_size": 0.01 if status == "EXECUTED" else 0.0,
            "slippage": 0.0, "spread_at_entry": 0.0001, "profit_loss": 0.0,
            "r_multiple": 0.0, "close_time": "2026-11-02T12:00:00Z"}


def apply(data, evidence):
    return db.apply_execution_feedback(data, dedupe_key=_execution_feedback_dedupe_key(data),
                                       source_file="execution_T1.json", evidence_context=evidence)


def snapshot(connection):
    with connection() as conn:
        return (conn.execute("SELECT status, trade_ticket FROM trades WHERE trade_id='T1'").fetchone()[:],
                conn.execute("SELECT state FROM trade_reservations WHERE trade_id='T1'").fetchone()[0],
                conn.execute("SELECT count(*) FROM trade_lifecycle_events").fetchone()[0],
                conn.execute("SELECT count(*) FROM feedback_receipts").fetchone()[0])


def test_execution_and_duplicate_are_atomic(ledger):
    connection, evidence = ledger
    assert apply(payload(), evidence) == "applied"
    expected = (("EXECUTED_OPEN", 123), "EXECUTED", 1, 1)
    assert snapshot(connection) == expected
    assert apply(payload(), evidence) == "duplicate"
    repeated = payload()
    repeated["close_time"] = "2026-11-02T12:00:01Z"
    assert apply(repeated, evidence) == "duplicate"
    assert snapshot(connection) == expected


def test_duplicate_receipt_with_changed_fill_is_a_conflict(ledger):
    connection, evidence = ledger
    assert apply(payload(), evidence) == "applied"
    changed = payload()
    changed["lot_size"] = 0.02
    assert apply(changed, evidence) == "conflict"
    assert db.has_unresolved_execution_feedback(evidence)
    assert snapshot(connection) == (("EXECUTED_OPEN", 123), "EXECUTED", 1, 1)


@pytest.mark.parametrize("table,operation", [("trades", "UPDATE"), ("trade_lifecycle_events", "INSERT"),
                                             ("trade_reservations", "UPDATE"), ("feedback_receipts", "INSERT")])
def test_failure_at_each_write_rolls_back(ledger, table, operation):
    connection, evidence = ledger
    with connection() as conn:
        conn.execute(f"CREATE TRIGGER fail_write BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        apply(payload(), evidence)
    assert snapshot(connection) == (("PENDING", None), "ROUTED", 0, 0)
    with connection() as conn:
        conn.execute("DROP TRIGGER fail_write")
    assert apply(payload(), evidence) == "applied"
    assert snapshot(connection) == (("EXECUTED_OPEN", 123), "EXECUTED", 1, 1)


def test_uncertain_send_retains_risk_until_execution_evidence(ledger):
    connection, evidence = ledger
    assert apply(payload("REJECTED_ORDER_SEND"), evidence) == "applied"
    assert snapshot(connection) == (("EXECUTION_UNCERTAIN", None), "EXECUTION_UNCERTAIN", 1, 1)
    assert db.has_unresolved_execution_feedback(evidence)
    assert apply(payload(), evidence) == "applied"
    assert not db.has_unresolved_execution_feedback(evidence)


def test_new_ea_uncertain_status_retains_reservation(ledger):
    connection, evidence = ledger
    assert apply(payload("EXECUTION_UNCERTAIN"), evidence) == "applied"
    assert snapshot(connection) == (("EXECUTION_UNCERTAIN", None), "EXECUTION_UNCERTAIN", 1, 1)
    assert db.has_unresolved_execution_feedback(evidence)


def test_ea_requires_confirmed_market_deal_before_executed_feedback():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "mt5_ea" / "FX_Execution.mq5").read_text()
    assert 'serverRetcode != TRADE_RETCODE_DONE || brokerDeal == 0' in source
    assert 'orderType == "LIMIT"' in source
    assert '"EXECUTION_UNCERTAIN"' in source
    assert 'server_retcode' in source and 'broker_deal' in source
    assert source.index('WriteTextFile(submissionMarker, tradeId)') < source.index('placed = trade.BuyLimit')
    assert 'FileIsExist(submissionMarker)' in source
    assert 'fileName != tradeId + ".json"' in source
    assert 'input bool RequireDemoAccount = true' in source
    assert 'AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO' in source
    assert 'ts = TimeGMT();' in source


def test_uncertain_send_cannot_be_resolved_by_later_rejection(ledger):
    _, evidence = ledger
    assert apply(payload("REJECTED_ORDER_SEND"), evidence) == "applied"
    assert apply(payload("REJECTED_SPREAD"), evidence) == "conflict"
    assert db.has_unresolved_execution_feedback(evidence)


def test_unresolved_feedback_halts_after_snapshot_refresh(ledger, tmp_path):
    _, evidence = ledger
    assert apply(payload("REJECTED_ORDER_SEND"), evidence) == "applied"
    engine = Engine.__new__(Engine)
    engine.feedback = SimpleNamespace(feedback_dir=tmp_path)
    engine.account_status = AccountStatus()
    engine.evidence_context = evidence
    engine._enforce_execution_feedback_integrity()
    assert engine.account_status.is_trading_halted
    engine.account_status.is_trading_halted = False
    engine._enforce_execution_feedback_integrity()
    assert engine.account_status.is_trading_halted


def test_rejection_and_contradictory_execution_are_recorded(ledger):
    connection, evidence = ledger
    assert apply(payload("REJECTED_SPREAD"), evidence) == "applied"
    assert apply(payload(), evidence) == "conflict"
    assert snapshot(connection) == (("REJECTED", None), "REJECTED", 1, 1)
    assert db.has_unresolved_execution_feedback(evidence)
    with connection() as conn:
        assert conn.execute("SELECT reason FROM execution_feedback_conflicts").fetchone()[0] == "RESERVATION_TRANSITION_CONFLICT:REJECTED->EXECUTED"
    assert not db.transition_trade_reservation("R1", "ROUTED", "late")


def test_unmatched_and_account_mismatch_halt_without_mutation(ledger):
    connection, evidence = ledger
    other = EvidenceContext("stream", "core", "mt5", "other")
    assert apply(payload(), other) == "unmatched"
    assert db.has_unresolved_execution_feedback(other)
    missing = payload()
    missing["trade_id"] = "MISSING"
    assert apply(missing, evidence) == "unmatched"
    assert db.has_unresolved_execution_feedback(evidence)
    assert snapshot(connection) == (("PENDING", None), "ROUTED", 0, 0)


@pytest.mark.parametrize("field,value", [("entry_price", 0), ("lot_size", -1), ("lot_size", float('nan')),
                                         ("spread_at_entry", -1), ("ticket", 0), ("position_ticket", True),
                                         ("status", "UNKNOWN")])
def test_invalid_execution_values(field, value):
    data = payload()
    data[field] = value
    with pytest.raises(SchemaError):
        validate_execution_feedback(data)


def test_engine_retry_after_commit_does_not_repeat_alert(ledger, tmp_path, monkeypatch):
    _, evidence = ledger
    artifact = tmp_path / "execution_T1.json"
    artifact.write_text("placeholder")
    engine = Engine.__new__(Engine)
    engine.tracer = SimpleNamespace(start_as_current_span=lambda *args: __import__('contextlib').nullcontext())
    engine.evidence_context = evidence
    engine.feedback = SimpleNamespace(collect_execution_feedback=lambda: [(artifact, payload())],
                                      collect_trade_exits=lambda: [], delete_artifact=Mock())
    engine.router = SimpleNamespace(release_lock=Mock())
    alert = Mock()
    monkeypatch.setattr("main.alert_trade_execution", alert)
    engine._consume_feedback()
    engine._consume_feedback()
    assert alert.call_count == 1
    assert engine.feedback.delete_artifact.call_count == 2


def test_orphan_and_expired_reservations_halt_until_resolved(ledger):
    connection, evidence = ledger
    with connection() as conn:
        conn.execute("UPDATE trade_reservations SET expires_at='2000-01-01T00:00:00+00:00'")
    assert db.has_unresolved_execution_feedback(evidence)
    with connection() as conn:
        conn.execute("UPDATE trade_reservations SET expires_at='2999-01-01T00:00:00+00:00'")
        conn.execute("DELETE FROM trades WHERE trade_id='T1'")
    assert db.has_unresolved_execution_feedback(evidence)


def test_housekeeping_marks_reservation_uncertain_atomically(ledger):
    connection, evidence = ledger
    db.mark_trade_execution_uncertain("T1", "ROUTER_PENDING_UNCERTAIN")
    assert snapshot(connection) == (("EXECUTION_UNCERTAIN", None), "EXECUTION_UNCERTAIN", 1, 0)
    assert db.has_unresolved_execution_feedback(evidence)


def test_inventory_reports_orphans_and_conflicts_read_only(ledger, tmp_path):
    from validation.reconciliation_inventory import build_inventory

    connection, evidence = ledger
    data = payload()
    data["trade_id"] = "MISSING"
    assert apply(data, evidence) == "unmatched"
    with connection() as conn:
        conn.execute("DELETE FROM trades WHERE trade_id='T1'")
    result = build_inventory(tmp_path / "state.db")
    assert not result["broker_verified"]
    assert result["active_reservations"][0]["trade_status"] is None
    assert result["unresolved_feedback_conflicts"][0]["trade_id"] == "MISSING"


def test_inventory_lists_bridge_artifacts_without_modifying_them(ledger, tmp_path):
    from validation.reconciliation_inventory import build_inventory
    bridge = tmp_path / "bridge"
    (bridge / "feedback" / "quarantine").mkdir(parents=True)
    (bridge / "pending_signals").mkdir()
    marker = bridge / "feedback" / "submission_T1.lock"
    marker.write_text("T1")
    (bridge / "pending_signals" / "T1.json").write_text("{}")
    (bridge / "feedback" / "quarantine" / "execution_T1.schema_invalid.json").write_text("{}")
    report = build_inventory(tmp_path / "state.db", bridge)
    assert report["bridge_artifacts"]["submission_markers"] == [marker.name]
    assert report["bridge_artifacts"]["pending_signals"] == ["T1.json"]
    assert report["bridge_artifacts"]["quarantined_feedback"] == ["execution_T1.schema_invalid.json"]
    assert marker.read_text() == "T1"


def test_position_query_failure_is_not_an_empty_snapshot(monkeypatch):
    from core import mt5_bridge

    fake = SimpleNamespace(account_info=lambda: SimpleNamespace(balance=1000, equity=1000, margin_free=1000,
                                                                currency="USD"), positions_get=lambda: None)
    monkeypatch.setattr(mt5_bridge, "mt5", fake)
    bridge = mt5_bridge.MT5Connection(1, "", "server")
    monkeypatch.setattr(bridge, "_ensure_connected", lambda: True)
    assert bridge.get_account_snapshot()["error"]["code"] == "MT5_POSITIONS_UNAVAILABLE"
    assert bridge.get_open_position_tickets() is None
    fake.positions_get = lambda: ()
    assert bridge.get_account_snapshot()["open_positions_count"] == 0
    assert bridge.get_open_position_tickets() == []


def test_broker_history_preserves_zero_valued_buy_direction(monkeypatch):
    from core import mt5_bridge
    monkeypatch.setattr(mt5_bridge, "mt5", SimpleNamespace(DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1,
        DEAL_ENTRY_INOUT=2, DEAL_ENTRY_OUT_BY=3, ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1))
    opening = SimpleNamespace(entry=0, type=0, ticket=1, order=10, position_id=20, volume=0.01,
                              price=1.1, time=1, symbol="EURUSD", comment="T1")
    closing = SimpleNamespace(entry=1, type=1, ticket=2, order=11, position_id=20, volume=0.01,
                              price=1.2, time=2, symbol="EURUSD", profit=1.0)
    summary = mt5_bridge._closed_history_summary_from_deals([opening, closing], trade_id_hint="T1")
    assert summary["direction"] == "BUY"
