from __future__ import annotations

import json

from bridge.execution_feedback import ExecutionFeedbackReader
from bridge.mock_feedback_simulator import MockFeedbackSimulator
from core.account_status import AccountStatus
from core.bridge_utils import get_mock_runtime_state_path


def test_mock_feedback_simulator_emits_execution_exit_and_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MT5_MOCK_OUTCOME_PATTERN", "WIN")
    pending_dir = tmp_path / "pending_signals"
    feedback_dir = tmp_path / "feedback"
    exits_dir = tmp_path / "exits"
    pending_dir.mkdir(parents=True, exist_ok=True)

    trade_id = "AI_mock_sim_001"
    (pending_dir / f"{trade_id}.json").write_text(
        json.dumps(
            {
                "trade_id": trade_id,
                "symbol": "EURUSD",
                "direction": "BUY",
                "risk_percent": 0.00005,
                "stop_pips": 10.0,
                "take_profit_pips": 22.0,
                "timestamp_utc": "2026-03-07T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    simulator = MockFeedbackSimulator(pending_dir=pending_dir, feedback_dir=feedback_dir, exits_dir=exits_dir)
    processed = simulator.process_pending(account_status=AccountStatus(balance=10000.0, equity=10000.0))

    assert processed == 1
    assert not (pending_dir / f"{trade_id}.json").exists()

    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir, exits_dir=exits_dir)
    execution = reader.consume_execution_feedback()
    exits = reader.consume_trade_exits()
    snapshot = reader.read_account_snapshot()

    assert len(execution) == 1
    assert execution[0]["trade_id"] == trade_id
    assert execution[0]["position_ticket"] > execution[0]["ticket"]

    assert len(exits) == 1
    assert exits[0]["trade_id"] == trade_id
    assert exits[0]["status"] == "CLOSED_WIN"
    assert exits[0]["is_final_exit"] is True
    assert exits[0]["r_multiple"] == 2.2

    assert snapshot is not None
    assert snapshot["snapshot_source"] == "mock_feedback_simulator"
    assert snapshot["balance"] > 10000.0
    assert snapshot["open_positions_count"] == 0

    simulator.clear_account_snapshot()
    assert reader.read_account_snapshot() is None


def test_mock_feedback_simulator_cycles_loss_breakeven_and_custom_win(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MT5_MOCK_OUTCOME_PATTERN", "LOSS,BREAKEVEN,1.5")
    monkeypatch.setenv("MT5_MOCK_START_BALANCE", "10000")

    pending_dir = tmp_path / "pending_signals"
    feedback_dir = tmp_path / "feedback"
    exits_dir = tmp_path / "exits"
    pending_dir.mkdir(parents=True, exist_ok=True)

    simulator = MockFeedbackSimulator(pending_dir=pending_dir, feedback_dir=feedback_dir, exits_dir=exits_dir)

    for idx in range(3):
        trade_id = f"AI_mock_seq_{idx}"
        (pending_dir / f"{trade_id}.json").write_text(
            json.dumps(
                {
                    "trade_id": trade_id,
                    "symbol": "EURUSD",
                    "direction": "BUY",
                    "risk_percent": 0.01,
                    "stop_pips": 10.0,
                    "take_profit_pips": 22.0,
                    "timestamp_utc": "2026-03-07T12:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        assert simulator.process_pending(account_status=AccountStatus(balance=10000.0, equity=10000.0)) == 1

    reader = ExecutionFeedbackReader(feedback_dir=feedback_dir, exits_dir=exits_dir)
    executions = reader.consume_execution_feedback()
    exits = reader.consume_trade_exits()
    snapshot = reader.read_account_snapshot()

    assert len(executions) == 3
    assert [item["status"] for item in exits] == ["CLOSED_LOSS", "CLOSED_BREAKEVEN", "CLOSED_WIN"]
    assert [item["r_multiple"] for item in exits] == [-1.0, 0.0, 1.5]
    assert [item["profit_loss"] for item in exits] == [-100.0, 0.0, 148.5]
    assert snapshot is not None
    assert snapshot["balance"] == 10048.5
    assert snapshot["consecutive_losses"] == 0

    state = json.loads(get_mock_runtime_state_path(tmp_path).read_text(encoding="utf-8"))
    assert state["outcome_index"] == 3
    assert state["balance"] == 10048.5