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

    # Default order_type to MARKET if not provided.
    payload.setdefault("order_type", "MARKET")
    if payload["order_type"] not in {"MARKET", "LIMIT"}:
        raise SchemaError("SignalPayload order_type must be MARKET or LIMIT")

    if payload["order_type"] == "LIMIT" and "limit_price" not in payload:
        raise SchemaError("SignalPayload limit_price required for LIMIT orders")

    # Validate tp_mode if present
    if "tp_mode" in payload and payload["tp_mode"] not in {"FIXED", "TRAIL"}:
        raise SchemaError("SignalPayload tp_mode must be FIXED or TRAIL")

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


def validate_trade_exit(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "ticket",
        "profit_loss",
        "status",
        "close_time",
    }
    _required(payload, required, "TradeExit")
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
    if "open_symbols" in payload and not isinstance(payload["open_symbols"], list):
        raise SchemaError("AccountSnapshot open_symbols must be a list when provided")
    return payload


def technical_signal_to_payload(signal: TechnicalSignal, risk_percent: float) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trade_id": signal.trade_id,
        "symbol": signal.symbol,
        "direction": signal.direction,
        "risk_percent": float(risk_percent),
        "stop_pips": float(signal.stop_pips),
        "take_profit_pips": float(signal.take_profit_pips),
        "timestamp_utc": signal.timestamp_utc or datetime.now(timezone.utc).isoformat(),
        "reason_code": signal.reason_code,
        "confidence": signal.confidence,
        "order_type": getattr(signal, "order_type", "MARKET") or "MARKET",
    }
    # Include limit_price if this is a LIMIT order.
    limit_price = getattr(signal, "limit_price", None)
    if limit_price is not None and limit_price > 0:
        payload["limit_price"] = float(limit_price)
    # Trade management parameters (Approach A + B)
    payload["be_trigger_r"] = float(signal.be_trigger_r)
    payload["partial_close_r"] = float(signal.partial_close_r)
    payload["trailing_atr_mult"] = float(signal.trailing_atr_mult)
    payload["tp_mode"] = signal.tp_mode
    if signal.structural_sl_pips is not None:
        payload["structural_sl_pips"] = float(signal.structural_sl_pips)
    return validate_signal_payload(payload)
