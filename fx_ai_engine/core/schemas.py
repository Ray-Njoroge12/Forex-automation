from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.types import TechnicalSignal


class SchemaError(ValueError):
    """Raised when payload schema validation fails."""


def _required(payload: dict[str, Any], fields: set[str], schema_name: str) -> None:
    missing = sorted(field for field in fields if field not in payload)
    if missing:
        raise SchemaError(f"{schema_name} missing required fields: {missing}")


def validate_signal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "trade_id",
        "symbol",
        "direction",
        "risk_percent",
        "stop_pips",
        "take_profit_pips",
        "timestamp_utc",
    }
    _required(payload, required, "SignalPayload")

    if payload["direction"] not in {"BUY", "SELL"}:
        raise SchemaError("SignalPayload direction must be BUY or SELL")

    if float(payload["risk_percent"]) <= 0:
        raise SchemaError("SignalPayload risk_percent must be > 0")

    if float(payload["stop_pips"]) <= 0 or float(payload["take_profit_pips"]) <= 0:
        raise SchemaError("SignalPayload stop/take profit pips must be > 0")

    return payload


def validate_execution_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "trade_id",
        "ticket",
        "status",
        "entry_price",
        "slippage",
        "spread_at_entry",
        "profit_loss",
        "r_multiple",
        "close_time",
    }
    _required(payload, required, "ExecutionFeedback")
    return payload


def validate_account_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "timestamp",
        "balance",
        "equity",
        "margin_free",
        "open_positions_count",
        "floating_pnl",
    }
    _required(payload, required, "AccountSnapshot")
    return payload


def technical_signal_to_payload(signal: TechnicalSignal, risk_percent: float) -> dict[str, Any]:
    payload = {
        "trade_id": signal.trade_id,
        "symbol": signal.symbol,
        "direction": signal.direction,
        "risk_percent": float(risk_percent),
        "stop_pips": float(signal.stop_pips),
        "take_profit_pips": float(signal.take_profit_pips),
        "timestamp_utc": signal.timestamp_utc or datetime.now(timezone.utc).isoformat(),
        "reason_code": signal.reason_code,
        "confidence": signal.confidence,
    }
    return validate_signal_payload(payload)
