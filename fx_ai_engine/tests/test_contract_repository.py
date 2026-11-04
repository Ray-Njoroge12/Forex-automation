"""Contract for ``application.repository.EngineRepository`` (August audit §2).

Every test runs against ``SqliteEngineRepository`` (temporary SQLite file) and
``InMemoryEngineRepository``. Both must give the same answers, so replay and
unit tests can swap one for the other. The signature test pins each interface
method to the ``database.db`` function it stands for.
"""
from __future__ import annotations

import importlib
import inspect
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from application.repository import EngineRepository, RiskRepository, RuntimeStateRepository, TradeRepository
from core.account_status import AccountStatus
from core.evidence import EvidenceContext
from core.types import TechnicalSignal
from database.repositories.memory import InMemoryEngineRepository
from database.repositories.sqlite import SqliteEngineRepository

EVIDENCE = EvidenceContext("runtime_mt5_core_srs", "core_srs", "mt5", "mt5:Contract:1")
OTHER = EvidenceContext("runtime_mt5_core_srs", "core_srs", "mt5", "mt5:Contract:2")


def _interface_methods() -> list[str]:
    names: set[str] = set()
    for protocol in (TradeRepository, RiskRepository, RuntimeStateRepository):
        names.update(name for name, value in vars(protocol).items()
                     if callable(value) and not name.startswith("_"))
    return sorted(names)


def _unbound_signature(function) -> str:
    signature = inspect.signature(function)
    parameters = list(signature.parameters.values())
    if parameters and parameters[0].name == "self":
        parameters = parameters[1:]
    return str(signature.replace(parameters=parameters))


@pytest.fixture(params=["sqlite", "memory"])
def repo(request, tmp_path, monkeypatch):
    """Return (repository, rows) where rows(table) lists that table's rows in insertion order."""
    if request.param == "memory":
        memory = InMemoryEngineRepository()
        return memory, lambda table: [dict(row) for row in getattr(memory, table)]

    from database import connection, db

    schema_path = tmp_path / "schema.sql"
    shutil.copyfile(connection.SCHEMA_PATH, schema_path)
    monkeypatch.setattr(connection, "DB_PATH", tmp_path / "repo.db")
    monkeypatch.setattr(connection, "SCHEMA_PATH", schema_path)
    db.run_all_migrations()

    def rows(table: str) -> list[dict]:
        with connection.get_conn() as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    return SqliteEngineRepository(), rows


def _signal(trade_id: str = "T1", symbol: str = "EURUSD") -> TechnicalSignal:
    return TechnicalSignal(
        trade_id=trade_id, symbol=symbol, direction="BUY", stop_pips=10.0, take_profit_pips=22.0,
        risk_reward=2.2, confidence=0.7, reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc="2026-11-03T16:30:00+00:00", rsi_at_entry=55.0, spread_entry=0.4,
    )


def _reserve(repository, trade_id: str = "T1", reservation_id: str = "R1", *, risk: float = 0.032,
             expires_in: timedelta = timedelta(minutes=10), broker_positions: int = 0,
             broker_risk: float = 0.0) -> dict:
    return repository.try_create_trade_reservation(
        reservation_id=reservation_id, trade_id=trade_id, symbol="EURUSD", risk_percent=risk,
        policy_hash="contract", expires_at=(datetime.now(timezone.utc) + expires_in).isoformat(),
        max_positions=2, max_combined_exposure=0.05, broker_open_positions=broker_positions,
        broker_open_risk=broker_risk,
    )


def _execution(trade_id: str = "T1", status: str = "EXECUTED") -> dict:
    executed = status == "EXECUTED"
    return {"trade_id": trade_id, "ticket": 123 if executed else 0, "position_ticket": 456 if executed else 0,
            "status": status, "entry_price": 1.1 if executed else 0.0, "lot_size": 0.5 if executed else 0.0,
            "slippage": 0.0, "spread_at_entry": 0.0001, "profit_loss": 0.0, "r_multiple": 0.0,
            "close_time": "2026-11-03T16:30:05Z"}


def _propose(repository, trade_id: str = "T1", evidence: EvidenceContext = EVIDENCE) -> None:
    repository.insert_trade_proposal(_signal(trade_id), "PENDING", "ROUTED_TO_MT5", 0.032, "TRENDING_BULL",
                                     evidence_context=evidence, regime_confidence=0.8, is_london_session=1)


def _state(rows, trade_id: str = "T1") -> str:
    return next(row["state"] for row in rows("trade_reservations") if row["trade_id"] == trade_id)


def test_interface_matches_database_functions_and_both_implementations():
    db = importlib.import_module("database.db")
    names = _interface_methods()
    assert len(names) == 21
    for name in names:
        expected = str(inspect.signature(getattr(db, name)))
        assert _unbound_signature(getattr(EngineRepository, name)) == expected, name
        assert _unbound_signature(getattr(SqliteEngineRepository, name)) == expected, name
        assert _unbound_signature(getattr(InMemoryEngineRepository, name)) == expected, name
    assert isinstance(SqliteEngineRepository(), EngineRepository)
    assert isinstance(InMemoryEngineRepository(), EngineRepository)


def test_reservation_capacity_and_transitions(repo):
    repository, rows = repo
    assert _reserve(repository, risk=0.0)["reason_code"] == "RESERVATION_INPUT_INVALID"
    first = _reserve(repository)
    assert (first["approved"], first["reason_code"], first["projected_risk"]) == (True, "RESERVATION_APPROVED", 0.032)
    assert _reserve(repository)["reason_code"] == "RESERVATION_DUPLICATE"
    assert _reserve(repository, "T2", "R2")["reason_code"] == "RESERVATION_MAX_EXPOSURE"
    assert _reserve(repository, "T2", "R2", risk=0.01, broker_positions=1)["reason_code"] == "RESERVATION_MAX_POSITIONS"
    assert _reserve(repository, "T2", "R2", risk=0.01)["approved"] is True

    assert repository.transition_trade_reservation("R1", "ROUTED", "ROUTED_TO_MT5")
    assert repository.transition_trade_reservation_by_trade_id("T1", "EXECUTED", "EXECUTED")
    assert not repository.transition_trade_reservation("R1", "ROUTED", "late")
    assert not repository.transition_trade_reservation_by_trade_id("T1", "RELEASED", "late")
    assert repository.transition_trade_reservation_by_trade_id("T1", "CLOSED", "CLOSED_WIN")
    assert not repository.transition_trade_reservation("R_MISSING", "ROUTED", "x")
    assert [(row["trade_id"], row["state"], row["reason_code"]) for row in rows("trade_reservations")] == [
        ("T1", "CLOSED", "CLOSED_WIN"), ("T2", "RESERVED", "RESERVATION_APPROVED")]
    with pytest.raises(ValueError):
        repository.transition_trade_reservation("R1", "BOGUS", "x")
    with pytest.raises(ValueError):
        repository.transition_trade_reservation_by_trade_id("T1", "ROUTED", "x")


def test_proposal_execution_exit_round_trip(repo):
    repository, rows = repo
    _propose(repository)
    _propose(repository)  # upsert: still one row
    assert len(rows("trades")) == 1
    trade = rows("trades")[0]
    assert (trade["status"], trade["stop_loss"], trade["take_profit"], trade["lot_size"],
            trade["spread_signal_pips"], trade["regime_confidence"], trade["is_london_session"]) == (
        "PENDING", 10.0, 22.0, 0.0, 0.4, 0.8, 1)
    assert _reserve(repository)["approved"] is True
    assert repository.transition_trade_reservation("R1", "ROUTED", "ROUTED_TO_MT5")

    payload = _execution()
    kwargs = {"dedupe_key": "k1", "source_file": "execution_T1.json", "evidence_context": EVIDENCE}
    assert repository.apply_execution_feedback(payload, **kwargs) == "applied"
    assert repository.apply_execution_feedback(payload, **kwargs) == "duplicate"
    assert repository.apply_execution_feedback(payload, **{**kwargs, "dedupe_key": "k2"}) == "duplicate"
    changed = {**payload, "entry_price": 1.2}
    assert repository.apply_execution_feedback(changed, **{**kwargs, "dedupe_key": "k3"}) == "conflict"
    assert repository.has_unresolved_execution_feedback(EVIDENCE)
    assert not repository.has_unresolved_execution_feedback(OTHER)

    open_trades = repository.list_open_trades(evidence_stream=EVIDENCE.evidence_stream,
                                              account_scope=EVIDENCE.account_scope)
    assert [(t["trade_id"], t["status"], t["trade_ticket"], t["position_ticket"], t["entry_price"], t["lot_size"],
             t["risk_percent"]) for t in open_trades] == [("T1", "EXECUTED_OPEN", 123, 456, 1.1, 0.5, 0.032)]
    assert repository.list_open_trades(account_scope=OTHER.account_scope) == []
    assert repository.get_open_trade_ledger(evidence_stream=EVIDENCE.evidence_stream,
                                            account_scope=EVIDENCE.account_scope) == {
        "open_trade_count": 1, "open_risk_percent": 0.032, "open_symbols": ["EURUSD"], "open_trade_ids": ["T1"],
        "open_trade_tickets": [123], "open_position_tickets": [456], "open_statuses": ["EXECUTED_OPEN"]}
    assert _state(rows) == "EXECUTED"
    assert [(r["feedback_kind"], r["dedupe_key"], r["ticket"]) for r in rows("feedback_receipts")] == [
        ("execution", "k1", 123)]

    assert repository.update_trade_exit_result({"trade_id": "T1", "is_final_exit": False}) is False
    exit_payload = {"trade_id": "T1", "ticket": 123, "position_ticket": 456, "status": "CLOSED",
                    "profit_loss": 40.0, "r_multiple": 0.8, "close_time": "2026-11-03T18:00:00+00:00"}
    assert not repository.update_trade_exit_result(exit_payload, evidence_context=OTHER)
    assert repository.update_trade_exit_result(exit_payload, evidence_context=EVIDENCE)
    assert repository.transition_trade_reservation_by_trade_id("T1", "CLOSED", "CLOSED")
    assert repository.list_open_trades() == []
    row = repository.get_trade_by_identifiers(position_ticket=456, evidence_context=EVIDENCE)
    assert row == {"trade_id": "T1", "symbol": "EURUSD", "direction": "BUY", "stop_loss": 10.0,
                   "take_profit": 22.0, "lot_size": 0.5, "entry_price": 1.1, "r_multiple": 0.8, "status": "CLOSED"}
    assert repository.get_trade_by_identifiers() is None
    assert repository.get_trade_by_identifiers(trade_id="T_MISSING") is None
    assert [(e["event_type"], e["status"], e["reason_code"]) for e in rows("trade_lifecycle_events")] == [
        ("PROPOSAL", "PENDING", "ROUTED_TO_MT5"), ("PROPOSAL", "PENDING", "ROUTED_TO_MT5"),
        ("EXECUTION", "EXECUTED_OPEN", "EXECUTED"), ("EXIT", "CLOSED", "CLOSED")]


def test_exit_status_from_pnl_and_ticket_fallbacks(repo):
    repository, rows = repo
    for trade_id in ("T_WIN", "T_LOSS", "T_FLAT"):
        _propose(repository, trade_id)
    assert repository.update_trade_execution_result({"trade_id": "T_WIN", "ticket": 11, "status": "EXECUTED",
                                                     "entry_price": 1.1, "lot_size": 0.5})
    assert repository.update_trade_execution_result({"trade_id": "T_LOSS", "ticket": 12, "position_ticket": 22,
                                                     "status": "EXECUTED", "lot_size": 0.0})
    assert not repository.update_trade_execution_result({"trade_id": "T_MISSING", "ticket": 1, "status": "EXECUTED"})
    assert repository.update_trade_exit_result({"ticket": 11, "status": "TP", "profit_loss": 5.0})
    assert repository.update_trade_exit_result({"position_ticket": 22, "status": "SL", "profit_loss": -5.0,
                                                "r_multiple": -1.0}, evidence_context=EVIDENCE)
    assert repository.update_trade_exit_result({"trade_id": "T_FLAT", "status": "MANUAL", "profit_loss": 0.0})
    by_id = {row["trade_id"]: row for row in rows("trades")}
    assert [(by_id[t]["status"], by_id[t]["trade_ticket"], by_id[t]["position_ticket"], by_id[t]["lot_size"],
             by_id[t]["r_multiple"]) for t in ("T_WIN", "T_LOSS", "T_FLAT")] == [
        ("CLOSED_WIN", 11, 11, 0.5, 0.0), ("CLOSED_LOSS", 12, 22, 0.0, -1.0), ("CLOSED_BREAKEVEN", None, None, 0.0, 0.0)]
    # Exit events are recorded only when the payload names the trade.
    assert [e["trade_id"] for e in rows("trade_lifecycle_events") if e["event_type"] == "EXIT"] == ["T_FLAT"]


def test_rejection_and_uncertain_execution(repo):
    repository, rows = repo
    _propose(repository, "T_REJ")
    assert _reserve(repository, "T_REJ", "R_REJ")["approved"] is True
    rejected = _execution("T_REJ", "REJECTED_SPREAD")
    kwargs = {"source_file": "execution_T_REJ.json", "evidence_context": EVIDENCE}
    assert repository.apply_execution_feedback(rejected, dedupe_key="k-rej", **kwargs) == "applied"
    assert repository.apply_execution_feedback(rejected, dedupe_key="k-rej-2", **kwargs) == "duplicate"
    assert _state(rows, "T_REJ") == "REJECTED"
    trade = rows("trades")[0]
    assert (trade["status"], trade["reason_code"], trade["trade_ticket"], trade["execution_time"]) == (
        "REJECTED", "REJECTED_SPREAD", None, None)

    _propose(repository, "T_UNC")
    assert _reserve(repository, "T_UNC", "R_UNC")["approved"] is True
    assert not repository.has_unresolved_execution_feedback(EVIDENCE)
    repository.mark_trade_execution_uncertain("T_UNC", "ROUTER_PENDING_UNCERTAIN")
    repository.mark_trade_execution_uncertain("T_REJ", "ROUTER_PENDING_UNCERTAIN")  # terminal: no change
    assert (_state(rows, "T_UNC"), _state(rows, "T_REJ")) == ("EXECUTION_UNCERTAIN", "REJECTED")
    assert repository.has_unresolved_execution_feedback(EVIDENCE)
    assert not repository.has_unresolved_execution_feedback(OTHER)
    assert [t["trade_id"] for t in repository.list_open_trades()] == ["T_UNC"]
    uncertain = _execution("T_UNC", "EXECUTION_UNCERTAIN")
    assert repository.apply_execution_feedback(uncertain, dedupe_key="k-unc", **kwargs) == "applied"
    assert repository.apply_execution_feedback(_execution("T_UNC"), dedupe_key="k-unc-exec", **kwargs) == "applied"
    assert _state(rows, "T_UNC") == "EXECUTED"
    assert not repository.has_unresolved_execution_feedback(EVIDENCE)


def test_unmatched_feedback_conflict_resolves_once_trade_exists(repo):
    repository, rows = repo
    kwargs = {"dedupe_key": "k-late", "source_file": "execution_T_LATE.json", "evidence_context": EVIDENCE}
    assert repository.apply_execution_feedback(_execution("T_LATE"), **kwargs) == "unmatched"
    assert repository.has_unresolved_execution_feedback(EVIDENCE)
    _propose(repository, "T_LATE")
    assert repository.apply_execution_feedback(_execution("T_LATE"), **kwargs) == "unmatched"
    assert [(c["trade_id"], c["reason"]) for c in rows("execution_feedback_conflicts")] == [
        ("T_LATE", "TRADE_NOT_FOUND_IN_EVIDENCE_SCOPE")]
    assert _reserve(repository, "T_LATE", "R_LATE")["approved"] is True
    assert repository.apply_execution_feedback(_execution("T_LATE"), **kwargs) == "applied"
    assert rows("execution_feedback_conflicts")[0]["resolved_at"] is not None
    assert not repository.has_unresolved_execution_feedback(EVIDENCE)


def test_expired_or_orphan_reservation_keeps_feedback_unresolved(repo):
    repository, _rows = repo
    _propose(repository, "T_OLD")
    assert _reserve(repository, "T_OLD", "R_OLD", expires_in=timedelta(minutes=-1))["approved"] is True
    assert repository.has_unresolved_execution_feedback(EVIDENCE)
    assert not repository.has_unresolved_execution_feedback(OTHER)
    assert repository.transition_trade_reservation("R_OLD", "RELEASED", "ROUTER_SEND_FAILED")
    assert not repository.has_unresolved_execution_feedback(EVIDENCE)
    # A reservation without a trade row is unresolved in every scope.
    assert _reserve(repository, "T_ORPHAN", "R_ORPHAN")["approved"] is True
    assert repository.has_unresolved_execution_feedback(OTHER)


def test_feedback_receipts(repo):
    repository, rows = repo
    assert not repository.feedback_receipt_exists("exit", "k-exit", evidence_context=EVIDENCE)
    assert repository.record_feedback_receipt("exit", "k-exit", trade_id="T", ticket=7, status="CLOSED",
                                              close_time="2026-11-03T18:00:00+00:00", source_file="exit_7.json",
                                              evidence_context=EVIDENCE) is True
    assert repository.record_feedback_receipt("exit", "k-exit", evidence_context=EVIDENCE) is False
    assert repository.record_feedback_receipt("exit", "k-zero", ticket=0, evidence_context=EVIDENCE) is True
    assert repository.feedback_receipt_exists("exit", "k-exit", evidence_context=EVIDENCE)
    assert not repository.feedback_receipt_exists("exit", "k-exit", evidence_context=OTHER)
    assert not repository.feedback_receipt_exists("execution", "k-exit", evidence_context=EVIDENCE)
    assert not repository.feedback_receipt_exists("exit", "k-exit")
    assert [(r["dedupe_key"], r["trade_id"], r["ticket"], r["source_file"]) for r in rows("feedback_receipts")] == [
        ("k-exit", "T", 7, "exit_7.json"), ("k-zero", None, None, None)]


def test_decision_records_and_account_metrics(repo):
    repository, rows = repo
    now = datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc)
    repository.insert_risk_event("HARD_RISK", "BLOCK", "contract", "T1", evidence_context=EVIDENCE)
    repository.insert_risk_event("STATE_STALE", "BLOCK", "no context")
    repository.insert_decision_funnel_event(decision_time=now, stage="REGIME", outcome="PASS",
                                            reason_code="REGIME_TREND_BULL", symbol="EURUSD",
                                            evidence_context=EVIDENCE)
    repository.insert_ml_shadow_event(decision_time=now, symbol="EURUSD", stage="POST_HARD_RISK",
                                      primary_gate_outcome="ROUTE", shadow_outcome="KEEP", reason_code="SHADOW",
                                      probability=0.6, threshold=0.5, model_loaded=True, evidence_context=EVIDENCE)
    assert [(r["rule_name"], r["severity"], r["trade_id"], r["account_scope"]) for r in rows("risk_events")] == [
        ("HARD_RISK", "BLOCK", "T1", EVIDENCE.account_scope), ("STATE_STALE", "BLOCK", None, "legacy_unpartitioned")]
    assert [(r["stage"], r["outcome"], r["reason_code"], r["symbol"], r["decision_time"], r["details"])
            for r in rows("decision_funnel_events")] == [
        ("REGIME", "PASS", "REGIME_TREND_BULL", "EURUSD", now.isoformat(), "")]
    assert [(r["shadow_outcome"], r["probability"], r["threshold"], r["model_loaded"])
            for r in rows("ml_shadow_events")] == [("KEEP", 0.6, 0.5, 1)]

    assert repository.get_latest_account_metric(evidence_stream=EVIDENCE.evidence_stream) is None
    for minutes, losses in ((0, 2), (5, 3)):
        repository.insert_account_metrics(
            AccountStatus(balance=100000.0, equity=100050.0 + minutes, consecutive_losses=losses,
                          is_trading_halted=minutes == 5, updated_at=now + timedelta(minutes=minutes)),
            evidence_context=EVIDENCE)
    repository.insert_account_metrics(AccountStatus(balance=1.0, equity=1.0, updated_at=now - timedelta(days=1)),
                                      evidence_context=OTHER)
    metric = repository.get_latest_account_metric(evidence_stream=EVIDENCE.evidence_stream,
                                                  account_scope=EVIDENCE.account_scope)
    assert (metric["balance"], metric["equity"], metric["consecutive_losses"], metric["is_trading_halted"],
            metric["timestamp"]) == (100000.0, 100055.0, 3, 1, (now + timedelta(minutes=5)).isoformat())
    assert repository.get_latest_account_metric(account_scope=OTHER.account_scope)["balance"] == 1.0
    assert repository.get_latest_account_metric()["equity"] == 100055.0


def test_engine_runtime_state(repo):
    repository, _rows = repo
    assert repository.get_engine_runtime_state("k", evidence_context=EVIDENCE) is None
    repository.set_engine_runtime_state("k", "v1", evidence_context=EVIDENCE)
    repository.set_engine_runtime_state("k", "v2", evidence_context=EVIDENCE)
    assert repository.get_engine_runtime_state("k", evidence_context=EVIDENCE) == "v2"
    assert repository.get_engine_runtime_state("k", evidence_context=OTHER) is None
