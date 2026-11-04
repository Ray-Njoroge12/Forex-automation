"""EA rejection statuses REJECTED_SYMBOL, REJECTED_ACCOUNT_MODE and REJECTED_LIVE_GUARD (C1)."""
from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bridge.execution_feedback import ExecutionFeedbackReader
from core.schemas import SchemaError, validate_execution_feedback
from database import db
from main import Engine
from tests.test_execution_feedback_integrity import apply, ledger, payload, snapshot  # noqa: F401  (ledger is a fixture)

NEW_STATUSES = ["REJECTED_SYMBOL", "REJECTED_ACCOUNT_MODE", "REJECTED_LIVE_GUARD"]


@pytest.mark.parametrize("status", NEW_STATUSES)
def test_schema_accepts_with_ticket_zero_and_rejects_ticket(status):
    data = payload(status)
    assert validate_execution_feedback(data) is data
    data["ticket"] = 5
    with pytest.raises(SchemaError):
        validate_execution_feedback(data)


@pytest.mark.parametrize("status", NEW_STATUSES)
def test_apply_marks_trade_and_reservation_rejected(ledger, status):  # noqa: F811
    connection, evidence = ledger
    assert apply(payload(status), evidence) == "applied"
    assert snapshot(connection) == (("REJECTED", None), "REJECTED", 1, 1)
    with connection() as conn:
        assert conn.execute("SELECT reason_code FROM trades WHERE trade_id='T1'").fetchone()[0] == status
        assert conn.execute("SELECT reason_code FROM trade_reservations WHERE trade_id='T1'").fetchone()[0] == status
    assert not db.has_unresolved_execution_feedback(evidence)
    assert apply(payload(status), evidence) == "duplicate"


@pytest.mark.parametrize("status", NEW_STATUSES)
def test_contradictory_execution_after_rejection_is_a_conflict(ledger, status):  # noqa: F811
    connection, evidence = ledger
    assert apply(payload(status), evidence) == "applied"
    assert apply(payload(), evidence) == "conflict"
    assert snapshot(connection) == (("REJECTED", None), "REJECTED", 1, 1)
    assert db.has_unresolved_execution_feedback(evidence)


@pytest.mark.parametrize("status", NEW_STATUSES)
def test_reader_collects_new_status_without_quarantine(tmp_path, status):
    feedback_dir = tmp_path / "feedback"
    reader = ExecutionFeedbackReader(feedback_dir, tmp_path / "exits")
    artifact = feedback_dir / "execution_T1.json"
    # Currency evidence is required before any artifact is processed (BR-001).
    (feedback_dir / "account_snapshot.json").write_text(json.dumps({"currency": "USD"}))
    artifact.write_text(json.dumps(payload(status)))
    collected = reader.collect_execution_feedback()
    assert [(path, data["status"]) for path, data in collected] == [(artifact, status)]
    assert artifact.exists()
    assert not (feedback_dir / "quarantine").exists()


@pytest.mark.parametrize("status", NEW_STATUSES)
def test_engine_consume_releases_router_lock(ledger, tmp_path, monkeypatch, status):  # noqa: F811
    _, evidence = ledger
    artifact = tmp_path / "execution_T1.json"
    artifact.write_text("placeholder")
    engine = Engine.__new__(Engine)
    engine.tracer = SimpleNamespace(start_as_current_span=lambda *args: nullcontext())
    engine.evidence_context = evidence
    engine.feedback = SimpleNamespace(collect_execution_feedback=lambda: [(artifact, payload(status))],
                                      collect_trade_exits=lambda: [], delete_artifact=Mock())
    engine.router = SimpleNamespace(release_lock=Mock())
    monkeypatch.setattr("application.feedback_service.alert_trade_execution", Mock())
    engine._consume_feedback()
    engine.router.release_lock.assert_called_once_with("T1")
    engine.feedback.delete_artifact.assert_called_once_with(artifact)
