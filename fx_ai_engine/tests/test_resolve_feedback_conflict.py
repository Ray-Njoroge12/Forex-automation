"""Operator resolution of execution-feedback conflicts."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from core.evidence import EvidenceContext
from database import connection as db_connection
from database import db


@pytest.fixture
def conn_factory(tmp_path, monkeypatch):
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
    with connection() as conn:
        conn.execute("""INSERT INTO execution_feedback_conflicts
            (evidence_stream, account_scope, dedupe_key, trade_id, reason, payload_json, created_at)
            VALUES ('runtime_mt5_core_srs', 'mt5:MetaQuotes-Demo:1', 'k', 'DEMO_X',
                    'TRADE_NOT_FOUND_IN_EVIDENCE_SCOPE', '{}', '2026-11-03T00:00:00Z')""")
    return connection


EVIDENCE = EvidenceContext("runtime_mt5_core_srs", "core_srs", "mt5", "mt5:MetaQuotes-Demo:1")


def test_resolution_requires_evidence_note(conn_factory):
    with pytest.raises(ValueError):
        db.resolve_execution_feedback_conflict(1, operator_note="ok")
    assert db.has_unresolved_execution_feedback(EVIDENCE)


def test_resolution_clears_halt_condition_and_audits(conn_factory):
    assert [c["trade_id"] for c in db.list_unresolved_execution_feedback_conflicts()] == ["DEMO_X"]
    assert db.resolve_execution_feedback_conflict(1, operator_note="test payload; no order or deal in MT5 history")
    assert not db.has_unresolved_execution_feedback(EVIDENCE)
    assert db.resolve_execution_feedback_conflict(1, operator_note="second attempt is a no-op here") is False
    with conn_factory() as conn:
        row = conn.execute("SELECT rule_name, policy_mode, execution_mode, account_scope FROM risk_events").fetchone()
    assert tuple(row) == ("FEEDBACK_CONFLICT_RESOLVED", "core_srs", "mt5", "mt5:MetaQuotes-Demo:1")
