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
        "management_state_restored": True,
        "managed_positions_count": 1,
        "managed_position_tickets": [1880903],
        "unmanaged_position_tickets": [],
    }
    (feedback_dir / "account_snapshot.json").write_text(json.dumps(payload), encoding="utf-8")

    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir)
    row = reader.read_account_snapshot()

    assert row is not None
    assert row["equity"] == 999.0
    assert row["management_state_restored"] is True
    assert row["managed_position_tickets"] == [1880903]


def test_trade_exit_feedback_accepts_position_ticket_and_trade_id(tmp_path) -> None:
    feedback_dir = tmp_path / "feedback"
    exits_dir = tmp_path / "exits"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    exits_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "ticket": 880001,
        "position_ticket": 880001,
        "trade_id": "AI_close_001",
        "profit_loss": 18.5,
        "status": "CLOSED_WIN",
        "is_final_exit": True,
        "close_time": "2026-02-25T12:45:00+00:00",
    }
    path = exits_dir / "exit_one.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir, exits_dir=exits_dir)
    rows = reader.consume_trade_exits()

    assert len(rows) == 1
    assert rows[0]["position_ticket"] == 880001
    assert rows[0]["trade_id"] == "AI_close_001"
    assert not path.exists()


def test_non_mock_feedback_reader_quarantines_mock_execution_exit_and_snapshot(tmp_path) -> None:
    feedback_dir = tmp_path / "feedback"
    exits_dir = tmp_path / "exits"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    exits_dir.mkdir(parents=True, exist_ok=True)

    (feedback_dir / "execution_mock.json").write_text(
        json.dumps(
            {
                "feedback_source": "mock_feedback_simulator",
                "trade_id": "AI_mock_exec",
                "ticket": 123,
                "status": "EXECUTED",
                "entry_price": 1.1,
                "slippage": 0.0,
                "spread_at_entry": 0.0001,
                "profit_loss": 0.0,
                "r_multiple": 0.0,
                "close_time": "2026-02-25T12:30:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (exits_dir / "exit_mock.json").write_text(
        json.dumps(
            {
                "feedback_source": "mock_feedback_simulator",
                "ticket": 123,
                "trade_id": "AI_mock_exec",
                "profit_loss": 5.0,
                "status": "CLOSED_WIN",
                "close_time": "2026-02-25T12:45:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (feedback_dir / "account_snapshot.json").write_text(
        json.dumps(
            {
                "snapshot_source": "mock_feedback_simulator",
                "timestamp": "2026-02-25T12:31:00+00:00",
                "balance": 1000.0,
                "equity": 1000.0,
                "margin_free": 1000.0,
                "open_positions_count": 0,
                "floating_pnl": 0.0,
            }
        ),
        encoding="utf-8",
    )

    reader = ExecutionFeedbackReader(
        feedback_dir=feedback_dir,
        exits_dir=exits_dir,
        allow_mock_artifacts=False,
    )

    assert reader.consume_execution_feedback() == []
    assert reader.consume_trade_exits() == []
    assert reader.read_account_snapshot() is None
    assert (feedback_dir / "quarantine" / "execution_mock.mock_source_blocked.json").exists()
    assert (exits_dir / "quarantine" / "exit_mock.mock_source_blocked.json").exists()
    assert (feedback_dir / "quarantine" / "account_snapshot.mock_source_blocked.json").exists()


def test_account_snapshot_schema_invalid_is_quarantined(tmp_path) -> None:
    feedback_dir = tmp_path / "feedback"
    exits_dir = tmp_path / "exits"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    exits_dir.mkdir(parents=True, exist_ok=True)

    (feedback_dir / "account_snapshot.json").write_text(
        json.dumps({"timestamp": "bad"}),
        encoding="utf-8",
    )

    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir, exits_dir=exits_dir)

    assert reader.read_account_snapshot() is None
    assert (feedback_dir / "quarantine" / "account_snapshot.schema_invalid.json").exists()


def test_account_snapshot_malformed_is_quarantined(tmp_path) -> None:
    feedback_dir = tmp_path / "feedback"
    exits_dir = tmp_path / "exits"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    exits_dir.mkdir(parents=True, exist_ok=True)

    (feedback_dir / "account_snapshot.json").write_text("{bad", encoding="utf-8")

    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir, exits_dir=exits_dir)

    assert reader.read_account_snapshot() is None
    assert (feedback_dir / "quarantine" / "account_snapshot.malformed.json").exists()
