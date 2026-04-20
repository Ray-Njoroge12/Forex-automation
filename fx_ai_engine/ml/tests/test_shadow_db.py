"""Tests for ML shadow telemetry DB migration and inserts."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.evidence import EvidenceContext
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


def test_migrate_add_ml_shadow_events_is_idempotent(tmp_path, monkeypatch) -> None:
    db_path = _patch_db(tmp_path, monkeypatch)
    db_mod.initialize_schema()

    db_mod.migrate_add_ml_shadow_events()
    db_mod.migrate_add_ml_shadow_events()

    with _temp_conn(db_path) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(ml_shadow_events)").fetchall()}

    expected = {
        "decision_time",
        "symbol",
        "stage",
        "primary_gate_outcome",
        "shadow_outcome",
        "reason_code",
        "probability",
        "threshold",
        "model_loaded",
        "feature_schema_version",
    }
    assert expected.issubset(cols)


def test_insert_ml_shadow_event_persists_row(tmp_path, monkeypatch) -> None:
    db_path = _patch_db(tmp_path, monkeypatch)
    db_mod.initialize_schema()
    db_mod.migrate_add_ml_shadow_events()

    evidence = EvidenceContext(
        evidence_stream="runtime_mock_core_srs",
        policy_mode="core_srs",
        execution_mode="mock",
        account_scope="mock",
    )
    decision_time = datetime.now(timezone.utc)

    db_mod.insert_ml_shadow_event(
        decision_time=decision_time,
        symbol="EURUSD",
        trade_id="trade-shadow-1",
        stage="POST_HARD_RISK",
        primary_gate_outcome="ROUTE",
        shadow_outcome="REJECT",
        reason_code="ML_SHADOW_REJECT",
        details="prob=0.420 threshold=0.550",
        probability=0.42,
        threshold=0.55,
        model_loaded=True,
        checkpoint_path="ml/artifacts/training/run_a/baseline_model.joblib",
        feature_schema_version="v1.0.0",
        evidence_context=evidence,
    )

    with _temp_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT evidence_stream, policy_mode, execution_mode, account_scope,
                   symbol, trade_id, stage, primary_gate_outcome, shadow_outcome,
                   reason_code, probability, threshold, model_loaded, feature_schema_version
              FROM ml_shadow_events
             WHERE trade_id = ?
            """,
            ("trade-shadow-1",),
        ).fetchone()

    assert row is not None
    assert row["evidence_stream"] == "runtime_mock_core_srs"
    assert row["policy_mode"] == "core_srs"
    assert row["execution_mode"] == "mock"
    assert row["account_scope"] == "mock"
    assert row["symbol"] == "EURUSD"
    assert row["stage"] == "POST_HARD_RISK"
    assert row["primary_gate_outcome"] == "ROUTE"
    assert row["shadow_outcome"] == "REJECT"
    assert row["reason_code"] == "ML_SHADOW_REJECT"
    assert abs(float(row["probability"]) - 0.42) < 1e-8
    assert abs(float(row["threshold"]) - 0.55) < 1e-8
    assert int(row["model_loaded"]) == 1
    assert row["feature_schema_version"] == "v1.0.0"
