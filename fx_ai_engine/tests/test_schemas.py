from __future__ import annotations

import pytest

from core.schemas import (
    SchemaError,
    validate_account_snapshot,
    validate_execution_feedback,
    validate_signal_payload,
)


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


def test_signal_payload_serialises_trade_management_fields() -> None:
    from core.schemas import technical_signal_to_payload
    from core.types import TechnicalSignal
    sig = TechnicalSignal(
        trade_id="AI_test2",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc="2026-03-05T12:00:00+00:00",
        be_trigger_r=0.8,
        partial_close_r=1.2,
        trailing_atr_mult=1.5,
        tp_mode="HYBRID",
        structural_sl_pips=11.5,
    )
    payload = technical_signal_to_payload(sig, risk_percent=0.032)
    assert payload["be_trigger_r"] == 0.8
    assert payload["partial_close_r"] == 1.2
    assert payload["trailing_atr_mult"] == 1.5
    assert payload["tp_mode"] == "HYBRID"
    assert payload["structural_sl_pips"] == 11.5


def test_validate_signal_payload_accepts_hybrid_tp_mode() -> None:
    from core.schemas import validate_signal_payload

    payload = validate_signal_payload({
        "trade_id": "x",
        "symbol": "EURUSD",
        "direction": "BUY",
        "risk_percent": 0.032,
        "stop_pips": 10.0,
        "take_profit_pips": 22.0,
        "timestamp_utc": "2026-03-05T12:00:00+00:00",
        "tp_mode": "HYBRID",
    })

    assert payload["tp_mode"] == "HYBRID"


def test_signal_payload_omits_structural_sl_when_none() -> None:
    from core.schemas import technical_signal_to_payload
    from core.types import TechnicalSignal
    sig = TechnicalSignal(
        trade_id="AI_test3",
        symbol="EURUSD",
        direction="SELL",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_SELL",
        timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    payload = technical_signal_to_payload(sig, risk_percent=0.032)
    assert "structural_sl_pips" not in payload


def test_signal_payload_accepts_micro_capital_risk_precision() -> None:
    from core.schemas import technical_signal_to_payload
    from core.types import TechnicalSignal

    sig = TechnicalSignal(
        trade_id="AI_test4",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc="2026-03-05T12:00:00+00:00",
    )

    payload = technical_signal_to_payload(sig, risk_percent=0.00005)

    assert payload["risk_percent"] == 0.00005


def test_validate_signal_payload_rejects_invalid_tp_mode() -> None:
    from core.schemas import SchemaError, validate_signal_payload
    with pytest.raises(SchemaError, match="tp_mode"):
        validate_signal_payload({
            "trade_id": "x",
            "symbol": "EURUSD",
            "direction": "BUY",
            "risk_percent": 0.032,
            "stop_pips": 10.0,
            "take_profit_pips": 22.0,
            "timestamp_utc": "2026-03-05T12:00:00+00:00",
            "tp_mode": "INVALID",
        })


def test_account_snapshot_optional_open_symbols_must_be_list() -> None:
    with pytest.raises(SchemaError):
        validate_account_snapshot(
            {
                "timestamp": "2026-03-05T12:00:00+00:00",
                "balance": 1000.0,
                "equity": 995.0,
                "margin_free": 900.0,
                "open_positions_count": 1,
                "floating_pnl": -5.0,
                "open_symbols": "EURUSD",
            }
        )


def test_account_snapshot_accepts_management_restore_fields() -> None:
    payload = validate_account_snapshot(
        {
            "timestamp": "2026-03-05T12:00:00+00:00",
            "balance": 1000.0,
            "equity": 995.0,
            "margin_free": 900.0,
            "open_positions_count": 1,
            "floating_pnl": -5.0,
            "management_state_restored": True,
            "managed_positions_count": 1,
            "managed_position_tickets": [1880903],
            "unmanaged_position_tickets": [],
            "management_state_error": "",
        }
    )

    assert payload["management_state_restored"] is True


def test_account_snapshot_management_restore_fields_validate_types() -> None:
    with pytest.raises(SchemaError, match="management_state_restored"):
        validate_account_snapshot(
            {
                "timestamp": "2026-03-05T12:00:00+00:00",
                "balance": 1000.0,
                "equity": 995.0,
                "margin_free": 900.0,
                "open_positions_count": 1,
                "floating_pnl": -5.0,
                "management_state_restored": "yes",
            }
        )
