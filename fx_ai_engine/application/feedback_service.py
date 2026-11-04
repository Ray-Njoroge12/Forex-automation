"""EA feedback: execution acknowledgements, fills, exits and broker-history recovery.

Functions take the engine as their first argument. They call other engine
steps through the engine's methods so per-instance overrides keep working.
Persistence goes through ``engine.repository`` (``application.repository``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Mapping

from core.alerts import alert_trade_execution, alert_trade_exit

# Same logger name as before the split so log routing is unchanged.
logger = logging.getLogger("fx_ai_engine.main")


def _parse_timestamp_utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    # Backward compatibility for legacy EA payloads like "YYYY-MM-DD HH:MM:SS".
    for timestamp_format in ("%Y-%m-%d %H:%M:%S",):
        try:
            parsed_legacy = datetime.strptime(raw, timestamp_format)
            return parsed_legacy.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def _execution_feedback_dedupe_key(payload: Mapping[str, object]) -> str:
    ticket = _safe_int(payload.get("ticket"))
    return "|".join(
        (
            str(payload.get("trade_id", "") or ""),
            str(ticket),
            str(payload.get("status", "") or ""),
            str(payload.get("close_time", "") or ""),
        )
    )


def _exit_feedback_dedupe_key(payload: Mapping[str, object]) -> str:
    ticket = _safe_int(payload.get("position_ticket") or payload.get("ticket"))
    return "|".join(
        (
            str(payload.get("trade_id", "") or ""),
            str(ticket),
            str(payload.get("status", "") or ""),
            str(payload.get("close_time", "") or ""),
        )
    )


def _normalise_legacy_exit_close_time(payload: Mapping[str, object], bridge: object) -> dict[str, object]:
    """Return a copy of an exit payload with ``close_time`` in UTC.

    EA builds before 2026.11.03-3 wrote broker server time labelled ``Z`` and no
    ``close_time_server`` field. Only the file bridge knows the server offset, so
    other bridges (mock, Windows MT5Connection) are left unchanged. The deal
    history (converted to UTC by the EA) is preferred; otherwise the status
    file's server offset is subtracted. The original value is kept in
    ``close_time_reported``.
    """
    result = dict(payload)
    offset_fn = getattr(bridge, "server_utc_offset_seconds", None)
    if not callable(offset_fn) or "close_time_server" in payload:
        return result
    reported = payload.get("close_time")
    corrected: datetime | None = None
    position_ticket = _safe_int(payload.get("position_ticket") or payload.get("ticket"))
    if position_ticket > 0:
        try:
            summary = bridge.get_position_history_summary(position_ticket)
        except Exception as exc:
            logger.warning("Deal history lookup failed for legacy exit position_ticket=%s: %s", position_ticket, exc)
            summary = None
        if isinstance(summary, Mapping):
            corrected = _parse_timestamp_utc(summary.get("close_time"))
    if corrected is None:
        reported_utc = _parse_timestamp_utc(reported)
        offset = offset_fn()
        if reported_utc is not None and type(offset) is int:
            corrected = reported_utc - timedelta(seconds=offset)
    if corrected is None:
        logger.warning(
            "Legacy exit close_time %s left unconverted (no deal history or server offset) trade_id=%s",
            reported,
            payload.get("trade_id", ""),
        )
        return result
    result["close_time"] = corrected.isoformat()
    result["close_time_reported"] = reported
    return result


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pip_size(symbol: str) -> float:
    return 0.01 if symbol.endswith("JPY") else 0.0001


def _estimate_recovered_r_multiple(
    trade_row: Mapping[str, object],
    broker_summary: Mapping[str, object],
) -> float | None:
    symbol = str(trade_row.get("symbol") or broker_summary.get("symbol") or "").strip().upper()
    direction = str(trade_row.get("direction") or broker_summary.get("direction") or "").strip().upper()
    if not symbol or direction not in {"BUY", "SELL"}:
        return None

    try:
        stop_pips = float(trade_row.get("stop_loss") or 0.0)
        entry_price = float(broker_summary.get("entry_price") or 0.0)
        close_price = float(broker_summary.get("close_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if stop_pips <= 0 or entry_price <= 0:
        return None

    stop_distance = stop_pips * _pip_size(symbol)
    if stop_distance <= 0:
        return None

    close_legs = broker_summary.get("close_legs")
    if isinstance(close_legs, list) and close_legs:
        try:
            initial_volume = float(broker_summary.get("lot_size") or trade_row.get("lot_size") or 0.0)
        except (TypeError, ValueError):
            initial_volume = 0.0

        weighted_r_sum = 0.0
        closed_volume = 0.0
        for leg in close_legs:
            if not isinstance(leg, Mapping):
                continue
            try:
                leg_price = float(leg.get("price") or 0.0)
                leg_volume = float(leg.get("volume") or 0.0)
            except (TypeError, ValueError):
                continue
            if leg_price <= 0 or leg_volume <= 0:
                continue
            price_move = leg_price - entry_price if direction == "BUY" else entry_price - leg_price
            weighted_r_sum += (price_move / stop_distance) * leg_volume
            closed_volume += leg_volume

        denominator = initial_volume if initial_volume > 0 else closed_volume
        if denominator > 0 and closed_volume > 0:
            return round(weighted_r_sum / denominator, 8)

    if close_price <= 0:
        return None

    price_move = close_price - entry_price if direction == "BUY" else entry_price - close_price
    return round(price_move / stop_distance, 8)


def enforce_execution_feedback_integrity(engine) -> None:
    quarantine = engine.feedback.feedback_dir / "quarantine"
    try:
        unresolved = engine.repository.has_unresolved_execution_feedback(engine.evidence_context)
    except Exception:
        logger.exception("Cannot verify execution feedback integrity")
        unresolved = True
    if (getattr(engine, "_execution_feedback_error", False) or getattr(engine, "_broker_recovery_error", False) or unresolved
            or (quarantine.exists() and any(quarantine.glob("execution_*.json")))):
        engine._fail_closed_bridge_uncertainty("unresolved execution feedback requires reconciliation")


def record_broker_close_time(engine, close_time_value: object) -> None:
    close_time_utc = _parse_timestamp_utc(close_time_value)
    if close_time_utc is None:
        return
    now_utc = datetime.now(timezone.utc)
    if close_time_utc > now_utc + timedelta(seconds=60):
        # EA builds before 2026.11.03-3 labelled broker server time as UTC.
        # A future close time would make every fresh snapshot look stale.
        logger.warning(
            "Broker close_time %s is in the future; clamping to now (server-time label?)",
            close_time_utc.isoformat(),
        )
        close_time_utc = now_utc
    if engine._latest_broker_close_time_utc is None or close_time_utc > engine._latest_broker_close_time_utc:
        engine._latest_broker_close_time_utc = close_time_utc


def is_snapshot_stale_after_broker_close(engine, snapshot: Mapping[str, object]) -> bool:
    if not snapshot or snapshot.get("error"):
        return False
    if engine._latest_broker_close_time_utc is None:
        return False
    snapshot_time_utc = _parse_timestamp_utc(snapshot.get("timestamp"))
    if snapshot_time_utc is None:
        return False
    return snapshot_time_utc < engine._latest_broker_close_time_utc


def fetch_broker_closure_summary(
    engine,
    *,
    local_position_ticket: int,
    trade_id: str,
) -> dict[str, object] | None:
    broker_summary: dict[str, object] | None = None
    if local_position_ticket > 0:
        try:
            broker_summary = engine.bridge.get_position_history_summary(local_position_ticket)
        except Exception as exc:
            logger.warning(
                "Failed fetching broker history for position_ticket=%s trade_id=%s: %s",
                local_position_ticket,
                trade_id,
                exc,
            )

    if broker_summary is None and trade_id:
        try:
            broker_summary = engine.bridge.get_trade_history_summary(trade_id)
        except Exception as exc:
            if local_position_ticket > 0:
                logger.warning(
                    "Failed fetching broker history by trade_id=%s after position lookup miss/failure: %s",
                    trade_id,
                    exc,
                )
            else:
                logger.warning(
                    "Failed fetching broker history for trade_id=%s without stored tickets: %s",
                    trade_id,
                    exc,
                )
    return broker_summary


def recover_missing_trade_closures_from_broker(engine) -> None:
    engine._broker_recovery_error = False
    open_trades = engine.repository.list_open_trades(
        evidence_stream=engine.evidence_context.evidence_stream,
        account_scope=engine.evidence_context.account_scope,
    )
    if not open_trades:
        return

    try:
        tickets = engine.bridge.get_open_position_tickets()
        if tickets is None:
            raise RuntimeError("broker position query unavailable")
        open_position_tickets = set(tickets)
    except Exception as exc:
        engine._broker_recovery_error = True
        logger.warning("Failed reading broker open positions during lifecycle recovery: %s", exc)
        return

    for trade in open_trades:
        local_position_ticket = int(trade.get("position_ticket") or trade.get("trade_ticket") or 0)
        trade_id = str(trade.get("trade_id") or "").strip()
        if local_position_ticket > 0 and local_position_ticket in open_position_tickets:
            continue

        broker_summary = engine._fetch_broker_closure_summary(
            local_position_ticket=local_position_ticket,
            trade_id=trade_id,
        )
        if not broker_summary and not trade_id:
            continue

        if not broker_summary or not broker_summary.get("close_time"):
            continue

        trade_id = str(trade_id or broker_summary.get("trade_id") or "").strip()
        if not trade_id:
            continue

        recovered_position_ticket = int(
            broker_summary.get("position_ticket") or local_position_ticket or 0
        )
        recovered_trade_ticket = int(
            broker_summary.get("trade_ticket") or trade.get("trade_ticket") or recovered_position_ticket
        )

        engine.repository.update_trade_execution_result(
            {
                "trade_id": trade_id,
                "ticket": recovered_trade_ticket,
                "position_ticket": recovered_position_ticket,
                "status": "EXECUTED",
                "entry_price": float(broker_summary.get("entry_price") or trade.get("entry_price") or 0.0),
                "lot_size": float(broker_summary.get("lot_size") or trade.get("lot_size") or 0.0),
                "slippage": 0.0,
                "spread_at_entry": 0.0,
                "execution_time": broker_summary.get("execution_time"),
            }
        )

        recovered_r = _estimate_recovered_r_multiple(trade, broker_summary)
        matched_trade = engine.repository.update_trade_exit_result(
            {
                "trade_id": trade_id,
                "ticket": recovered_trade_ticket,
                "position_ticket": recovered_position_ticket,
                "status": broker_summary.get("status", "CLOSED"),
                "profit_loss": float(broker_summary.get("profit_loss") or 0.0),
                "r_multiple": recovered_r,
                "close_time": broker_summary.get("close_time"),
            },
            evidence_context=engine.evidence_context,
        )
        if not matched_trade:
            continue

        pnl = float(broker_summary.get("profit_loss") or 0.0)
        engine._record_consecutive_loss_update(pnl)
        engine._record_broker_close_time(broker_summary.get("close_time"))

        engine.router.release_lock(trade_id)
        recovery_reason = (
            "Recovered missing trade exit from MT5 broker history "
            f"trade_id={trade_id} position_ticket={recovered_position_ticket} profit_loss={pnl:.2f}"
        )
        engine._insert_risk_event(
            "TRADE_LIFECYCLE_RECOVERED",
            "WARN",
            recovery_reason,
            trade_id,
        )
        logger.warning(recovery_reason)


def consume_feedback(engine) -> None:
    with engine.tracer.start_as_current_span("consume_feedback"):
        engine._execution_feedback_error = False
        # Collect execution artifacts first; delete only after durable DB success.
        for path, payload in engine.feedback.collect_execution_feedback():
            dedupe_key = _execution_feedback_dedupe_key(payload)
            try:
                outcome = engine.repository.apply_execution_feedback(
                    payload, dedupe_key=dedupe_key, source_file=path.name, evidence_context=engine.evidence_context
                )
            except Exception:
                engine._execution_feedback_error = True
                logger.exception(
                    "Failed to apply execution feedback; deferring source=%s trade_id=%s",
                    path,
                    payload.get("trade_id", ""),
                )
                continue

            if outcome in {"unmatched", "conflict"}:
                engine._execution_feedback_error = True
                logger.warning(
                    "Execution feedback %s; deferring source=%s trade_id=%s",
                    outcome,
                    path,
                    payload.get("trade_id", ""),
                )
                continue
            if outcome == "applied" and payload.get("status") == "EXECUTED":
                alert_trade_execution(
                    str(payload.get("trade_id", "")),
                    str(payload.get("symbol", "")),
                    str(payload.get("direction", "")),
                    float(payload.get("lot_size", 0.0)),
                    float(payload.get("entry_price", 0.0)),
                )
            if payload.get("status") not in {"REJECTED_ORDER_SEND", "EXECUTION_UNCERTAIN"}:
                engine.router.release_lock(str(payload.get("trade_id", "")))
            engine.feedback.delete_artifact(path)

        # Collect trade exit artifacts first; keep unmatched finals for later retry.
        for path, payload in engine.feedback.collect_trade_exits():
            dedupe_key = _exit_feedback_dedupe_key(payload)
            if engine.repository.feedback_receipt_exists(
                "exit",
                dedupe_key,
                evidence_context=engine.evidence_context,
            ):
                engine.feedback.delete_artifact(path)
                continue

            # The receipt key stays on the raw payload so a re-delivered raw
            # file still dedupes; DB rows get the UTC close time.
            payload = _normalise_legacy_exit_close_time(payload, engine.bridge)
            is_final_exit = payload.get("is_final_exit") is not False
            try:
                matched_trade = engine.repository.update_trade_exit_result(
                    payload,
                    evidence_context=engine.evidence_context,
                )
            except Exception:
                logger.exception(
                    "Failed to apply trade-exit feedback; deferring source=%s trade_id=%s",
                    path,
                    payload.get("trade_id", ""),
                )
                continue

            if is_final_exit and not matched_trade:
                logger.warning(
                    "Trade-exit feedback matched no trade row; deferring source=%s trade_id=%s",
                    path,
                    payload.get("trade_id", ""),
                )
                continue

            try:
                engine.repository.record_feedback_receipt(
                    "exit",
                    dedupe_key,
                    trade_id=str(payload.get("trade_id", "") or ""),
                    ticket=_safe_int(payload.get("position_ticket") or payload.get("ticket")),
                    status=str(payload.get("status", "") or ""),
                    close_time=str(payload.get("close_time", "") or ""),
                    source_file=path.name,
                    evidence_context=engine.evidence_context,
                )
            except Exception:
                logger.exception(
                    "Failed to persist trade-exit feedback receipt; deferring source=%s trade_id=%s",
                    path,
                    payload.get("trade_id", ""),
                )
                continue

            if not is_final_exit:
                engine.feedback.delete_artifact(path)
                continue

            engine.repository.transition_trade_reservation_by_trade_id(
                str(payload.get("trade_id", "")),
                "CLOSED",
                str(payload.get("status", "CLOSED")),
            )

            pnl = float(payload.get("profit_loss", 0.0))
            trade_row = engine.repository.get_trade_by_identifiers(
                trade_id=payload.get("trade_id"),
                position_ticket=payload.get("position_ticket") or payload.get("ticket"),
                trade_ticket=payload.get("ticket"),
                evidence_context=engine.evidence_context,
            )
            trade_for_alert = trade_row or payload
            alert_trade_exit(
                str(trade_for_alert.get("trade_id", "")),
                str(trade_for_alert.get("symbol", "")),
                pnl,
                r_multiple=_estimate_recovered_r_multiple(trade_for_alert, payload),
            )
            engine._record_consecutive_loss_update(pnl)
            engine._record_broker_close_time(payload.get("close_time"))
            engine.feedback.delete_artifact(path)
