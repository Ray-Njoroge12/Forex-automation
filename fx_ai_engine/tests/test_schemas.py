from __future__ import annotations

import pytest

from core.schemas import SchemaError, validate_execution_feedback, validate_signal_payload


def test_signal_schema_rejects_invalid_direction() -> None:
    with pytest.raises(SchemaError):
        validate_signal_payload(
            {
                "trade_id": "x",
                "symbol": "EURUSD",
                "direction": "HOLD",
                "risk_percent": 0.01,
                "stop_pips": 10,
                "take_profit_pips": 22,
                "timestamp_utc": "2026-02-25T12:00:00+00:00",
            }
        )


def test_execution_feedback_requires_ticket() -> None:
    with pytest.raises(SchemaError):
        validate_execution_feedback(
            {
                "trade_id": "x",
                "status": "EXECUTED",
                "entry_price": 1.1,
                "slippage": 0.0,
                "spread_at_entry": 0.0,
                "profit_loss": 0.0,
                "r_multiple": 0.0,
                "close_time": "2026-02-25T12:00:00+00:00",
            }
        )
