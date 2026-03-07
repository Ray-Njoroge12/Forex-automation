from __future__ import annotations

import json

from bridge.execution_feedback import ExecutionFeedbackReader


def test_feedback_reader_skips_malformed_and_reads_valid(tmp_path) -> None:
    feedback_dir = tmp_path / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)

    malformed = feedback_dir / "execution_bad.json"
    malformed.write_text("{not-json", encoding="utf-8")

    valid_payload = {
        "trade_id": "AI_20260225_120000_ab12cd",
        "ticket": 123456,
        "status": "EXECUTED",
        "entry_price": 1.1001,
        "slippage": 0.00002,
        "spread_at_entry": 0.0001,
        "profit_loss": 0.0,
        "r_multiple": 0.0,
        "close_time": "2026-02-25T12:15:00+00:00",
    }
    valid = feedback_dir / "execution_good.json"
    valid.write_text(json.dumps(valid_payload), encoding="utf-8")

    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir)
    rows = reader.read_execution_feedback()

    assert len(rows) == 1
    assert rows[0]["trade_id"] == valid_payload["trade_id"]
    assert not malformed.exists()
    assert (feedback_dir / "quarantine" / "execution_bad.malformed.json").exists()


def test_feedback_consume_removes_valid_execution_files(tmp_path) -> None:
    feedback_dir = tmp_path / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)

    valid_payload = {
        "trade_id": "AI_20260225_120000_cd34ef",
        "ticket": 654321,
        "status": "EXECUTED",
        "entry_price": 1.2001,
        "slippage": 0.00001,
        "spread_at_entry": 0.00009,
        "profit_loss": 0.0,
        "r_multiple": 0.0,
        "close_time": "2026-02-25T12:30:00+00:00",
    }
    path = feedback_dir / "execution_one.json"
    path.write_text(json.dumps(valid_payload), encoding="utf-8")

    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir)
    rows = reader.consume_execution_feedback()

    assert len(rows) == 1
    assert not path.exists()


def test_feedback_consume_quarantines_schema_invalid_execution_file(tmp_path) -> None:
    feedback_dir = tmp_path / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)

    invalid_payload = {
        "trade_id": "AI_20260225_120000_invalid",
        "status": "EXECUTED",
        "entry_price": 1.2001,
        "slippage": 0.00001,
        "spread_at_entry": 0.00009,
        "profit_loss": 0.0,
        "r_multiple": 0.0,
        "close_time": "2026-02-25T12:30:00+00:00",
    }
    path = feedback_dir / "execution_invalid.json"
    path.write_text(json.dumps(invalid_payload), encoding="utf-8")

    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir)
    rows = reader.consume_execution_feedback()

    assert rows == []
    assert not path.exists()
    assert (feedback_dir / "quarantine" / "execution_invalid.schema_invalid.json").exists()


def test_account_snapshot_validation(tmp_path) -> None:
    feedback_dir = tmp_path / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "timestamp": "2026-02-25T12:31:00+00:00",
        "balance": 1000.0,
        "equity": 999.0,
        "margin_free": 850.0,
        "open_positions_count": 1,
        "floating_pnl": -1.0,
    }
    (feedback_dir / "account_snapshot.json").write_text(json.dumps(payload), encoding="utf-8")

    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir)
    row = reader.read_account_snapshot()

    assert row is not None
    assert row["equity"] == 999.0
