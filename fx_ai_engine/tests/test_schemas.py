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


def test_technical_signal_has_trade_management_defaults() -> None:
    from core.types import TechnicalSignal
    sig = TechnicalSignal(
        trade_id="AI_test",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    assert sig.be_trigger_r == 1.0
    assert sig.partial_close_r == 1.5
    assert sig.trailing_atr_mult == 2.0
    assert sig.tp_mode == "FIXED"
    assert sig.structural_sl_pips is None
