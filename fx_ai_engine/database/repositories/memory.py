"""In-memory implementation of ``application.repository.EngineRepository``.

For replay and unit tests that drive the engine without a SQLite file. Each
method ports the SQL of the same-named function in
``database.repositories.{trades,risk,config}``: the same filters, state
transitions, return values and reason codes. Rows are plain dicts kept in
public lists named after the SQLite tables (``trades``, ``trade_reservations``,
``decision_funnel_events``, ...) so tests can read them directly; ``id`` gives
insertion order like SQLite's AUTOINCREMENT key.

Not reproduced: schema-level failures (UNIQUE ticket violations, locked
database) and SQLite type affinity (values are stored as passed).
``tests/test_contract_repository.py`` runs the same behaviour contract
against this class and ``SqliteEngineRepository``.
"""
from __future__ import annotations

import itertools
import json
import threading
from datetime import datetime, timezone
from typing import Any

from core.account_status import AccountStatus
from core.evidence import EvidenceContext
from core.types import TechnicalSignal
from database.migrations import _default_evidence_context
from database.repositories.risk import _ACTIVE_RESERVATION_STATES, _RESERVATION_PREDECESSORS
from database.repositories.trades import _positive_float, _positive_int

_ACCOUNT_METRIC_COLUMNS = (
    "balance", "equity", "open_risk_percent", "open_usd_exposure_count",
    "daily_loss_percent", "weekly_loss_percent", "drawdown_percent",
    "peak_equity", "daily_anchor_date", "daily_anchor_equity",
    "weekly_anchor_key", "weekly_anchor_equity",
    "consecutive_losses", "is_trading_halted", "timestamp",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _julian_utc(value: Any) -> datetime | None:
    """Parse a timestamp the way the SQL ``julianday()`` comparison uses it (naive means UTC)."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


class InMemoryEngineRepository:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        # One lock stands in for SQLite's BEGIN IMMEDIATE / single-writer rule.
        self._lock = threading.RLock()
        self.trades: list[dict[str, Any]] = []
        self.trade_lifecycle_events: list[dict[str, Any]] = []
        self.trade_reservations: list[dict[str, Any]] = []
        self.feedback_receipts: list[dict[str, Any]] = []
        self.execution_feedback_conflicts: list[dict[str, Any]] = []
        self.account_metrics: list[dict[str, Any]] = []
        self.risk_events: list[dict[str, Any]] = []
        self.decision_funnel_events: list[dict[str, Any]] = []
        self.ml_shadow_events: list[dict[str, Any]] = []
        self.engine_runtime_state: dict[tuple[str, str, str], dict[str, str]] = {}

    # Helpers ------------------------------------------------------------------
    def _trade(self, trade_id: Any) -> dict[str, Any] | None:
        return next((row for row in self.trades if row["trade_id"] == trade_id), None)

    def _reservation(self, *, trade_id: Any = None, reservation_id: Any = None) -> dict[str, Any] | None:
        for row in self.trade_reservations:
            if (trade_id is not None and row["trade_id"] == trade_id) or (
                reservation_id is not None and row["reservation_id"] == reservation_id
            ):
                return row
        return None

    def _receipt(self, evidence: EvidenceContext, feedback_kind: str, dedupe_key: str) -> dict[str, Any] | None:
        for row in self.feedback_receipts:
            if (row["evidence_stream"], row["account_scope"], row["feedback_kind"], row["dedupe_key"]) == (
                evidence.evidence_stream, evidence.account_scope, feedback_kind, dedupe_key
            ):
                return row
        return None

    def _record_lifecycle(self, trade_id: str, event_type: str, status: str, reason_code: str,
                          payload: dict[str, Any] | None = None) -> None:
        self.trade_lifecycle_events.append({
            "id": next(self._ids),
            "trade_id": trade_id,
            "event_type": event_type,
            "status": status,
            "reason_code": reason_code,
            "payload_json": json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str),
            "created_at": _now(),
        })

    @staticmethod
    def _evidence_columns(evidence: EvidenceContext) -> dict[str, str]:
        return {
            "evidence_stream": evidence.evidence_stream,
            "policy_mode": evidence.policy_mode,
            "execution_mode": evidence.execution_mode,
            "account_scope": evidence.account_scope,
        }

    # Trades -------------------------------------------------------------------
    def insert_trade_proposal(
        self,
        signal: TechnicalSignal,
        status: str,
        reason_code: str,
        risk_percent: float,
        market_regime: str,
        *,
        evidence_context: EvidenceContext | None = None,
        regime_confidence: float = 0.0,
        atr_ratio: float = 1.0,
        is_london_session: int = 0,
        is_newyork_session: int = 0,
        rate_differential: float = 0.0,
    ) -> None:
        evidence = evidence_context or _default_evidence_context()
        # Columns rewritten by ON CONFLICT(trade_id) DO UPDATE; lot_size, open_time,
        # tickets and execution results keep their stored values.
        updates = {
            **self._evidence_columns(evidence),
            "symbol": signal.symbol,
            "direction": signal.direction,
            "order_type": "MARKET",
            "stop_loss": signal.stop_pips,
            "take_profit": signal.take_profit_pips,
            "risk_percent": risk_percent,
            "market_regime": market_regime,
            "status": status,
            "reason_code": reason_code,
            "regime_confidence": regime_confidence,
            "rsi_at_entry": signal.rsi_at_entry,
            "atr_ratio": atr_ratio,
            "is_london_session": is_london_session,
            "is_newyork_session": is_newyork_session,
            "rate_differential": rate_differential,
            "risk_reward": signal.risk_reward,
            "rsi_slope": signal.rsi_slope,
            "spread_entry": signal.spread_entry,
            "spread_signal_pips": signal.spread_entry,
        }
        with self._lock:
            row = self._trade(signal.trade_id)
            if row is None:
                self.trades.append({
                    "id": next(self._ids),
                    "trade_id": signal.trade_id,
                    "trade_ticket": None,
                    "position_ticket": None,
                    "lot_size": 0.0,
                    "entry_price": None,
                    "profit_loss": 0.0,
                    "r_multiple": 0.0,
                    "spread_exec_price": None,
                    "slippage": None,
                    "execution_time": None,
                    "open_time": _now(),
                    "close_time": None,
                    **updates,
                })
            else:
                row.update(updates)
            self._record_lifecycle(
                signal.trade_id,
                "PROPOSAL",
                status,
                reason_code,
                {"symbol": signal.symbol, "risk_percent": risk_percent, "market_regime": market_regime},
            )

    def apply_execution_feedback(self, payload: dict[str, Any], *, dedupe_key: str, source_file: str,
                                 evidence_context: EvidenceContext) -> str:
        from core.schemas import validate_execution_feedback

        validate_execution_feedback(payload)
        trade_id, status = payload["trade_id"], payload["status"]
        now = _now()
        evidence = evidence_context
        with self._lock:
            trade = next((row for row in self.trades if row["trade_id"] == trade_id
                          and row["evidence_stream"] == evidence.evidence_stream
                          and row["account_scope"] == evidence.account_scope), None)
            reservation = self._reservation(trade_id=trade_id)

            def conflict(reason: str, result: str = "conflict") -> str:
                exists = any(
                    (row["evidence_stream"], row["account_scope"], row["dedupe_key"])
                    == (evidence.evidence_stream, evidence.account_scope, dedupe_key)
                    for row in self.execution_feedback_conflicts
                )
                if not exists:
                    self.execution_feedback_conflicts.append({
                        "id": next(self._ids),
                        "evidence_stream": evidence.evidence_stream,
                        "account_scope": evidence.account_scope,
                        "dedupe_key": dedupe_key,
                        "trade_id": trade_id,
                        "reason": reason,
                        "payload_json": json.dumps(payload, sort_keys=True, default=str),
                        "created_at": now,
                        "resolved_at": None,
                    })
                return result

            if self._receipt(evidence, "execution", dedupe_key) is not None:
                # Same retry check as the SQL version: compare retained evidence.
                rows = [row for row in self.trade_lifecycle_events
                        if row["trade_id"] == trade_id and row["event_type"] == "EXECUTION"]
                for row in sorted(rows, key=lambda item: item["id"], reverse=True):
                    previous = json.loads(row["payload_json"])
                    if (previous.get("status"), previous.get("ticket"), previous.get("close_time")) == (
                            status, payload["ticket"], payload["close_time"]):
                        if any(previous.get(key) != payload.get(key)
                               for key in ("entry_price", "lot_size", "position_ticket")):
                            return conflict("RECEIPT_EVIDENCE_CONFLICT")
                        break
                return "duplicate"
            if trade is None:
                return conflict("TRADE_NOT_FOUND_IN_EVIDENCE_SCOPE", "unmatched")
            if reservation is None:
                return conflict("RESERVATION_NOT_FOUND", "unmatched")

            state = reservation["state"]
            target = "EXECUTED" if status == "EXECUTED" else (
                "EXECUTION_UNCERTAIN" if status in {"REJECTED_ORDER_SEND", "EXECUTION_UNCERTAIN"} else "REJECTED")
            if state not in _RESERVATION_PREDECESSORS[target]:
                return conflict(f"RESERVATION_TRANSITION_CONFLICT:{state}->{target}")
            if state == target and target in {"EXECUTED", "REJECTED"}:
                same = (target == "EXECUTED" and trade["status"] == "EXECUTED_OPEN"
                        and trade["trade_ticket"] == payload["ticket"]
                        and trade["position_ticket"] == (payload.get("position_ticket") or payload["ticket"])
                        and trade["entry_price"] == payload["entry_price"]
                        and trade["lot_size"] == payload["lot_size"])
                same = same or (target == "REJECTED" and trade["status"] == "REJECTED"
                                and trade["reason_code"] == status)
                if not same:
                    return conflict("TERMINAL_EVIDENCE_CONFLICT")
                return "duplicate"
            if trade["status"] not in ("PENDING", "EXECUTION_UNCERTAIN"):
                return conflict(f"TRADE_STATE_CONFLICT:{trade['status']}")

            trade_status = {"EXECUTED": "EXECUTED_OPEN", "REJECTED": "REJECTED",
                            "EXECUTION_UNCERTAIN": "EXECUTION_UNCERTAIN"}[target]
            executed = target == "EXECUTED"
            trade.update({
                "status": trade_status,
                "reason_code": status,
                "trade_ticket": payload["ticket"] if executed else None,
                "position_ticket": (payload.get("position_ticket") or payload["ticket"]) if executed else None,
                "entry_price": payload["entry_price"] if executed else trade["entry_price"],
                "lot_size": payload["lot_size"] if executed else trade["lot_size"],
                "slippage": payload["slippage"],
                "spread_exec_price": payload["spread_at_entry"],
                "execution_time": (payload.get("execution_time") or payload["close_time"] or now) if executed else None,
            })
            self._record_lifecycle(trade_id, "EXECUTION", trade_status, status, payload)
            reservation.update({"state": target, "reason_code": status, "updated_at": now})
            self.feedback_receipts.append({
                "id": next(self._ids),
                "timestamp": now,
                **self._evidence_columns(evidence),
                "feedback_kind": "execution",
                "dedupe_key": dedupe_key,
                "trade_id": trade_id,
                "ticket": payload["ticket"] or None,
                "status": status,
                "close_time": payload["close_time"],
                "source_file": source_file,
            })
            for row in self.execution_feedback_conflicts:
                if ((row["evidence_stream"], row["account_scope"], row["dedupe_key"])
                        == (evidence.evidence_stream, evidence.account_scope, dedupe_key)
                        and row["reason"] in ("TRADE_NOT_FOUND_IN_EVIDENCE_SCOPE", "RESERVATION_NOT_FOUND")):
                    row["resolved_at"] = now
        return "applied"

    def has_unresolved_execution_feedback(self, evidence_context: EvidenceContext) -> bool:
        scope = (evidence_context.evidence_stream, evidence_context.account_scope)
        with self._lock:
            conflict = any((row["evidence_stream"], row["account_scope"]) == scope and row["resolved_at"] is None
                           for row in self.execution_feedback_conflicts)
            now = datetime.now(timezone.utc)
            uncertain = False
            for reservation in self.trade_reservations:
                if reservation["state"] not in _ACTIVE_RESERVATION_STATES:
                    continue
                trade = self._trade(reservation["trade_id"])
                expires_at = _julian_utc(reservation["expires_at"])
                if not (reservation["state"] == "EXECUTION_UNCERTAIN" or trade is None
                        or expires_at is None or expires_at <= now):
                    continue
                if trade is None or (trade["evidence_stream"], trade["account_scope"]) == scope:
                    uncertain = True
                    break
        return bool(conflict or uncertain)

    def update_trade_execution_result(self, payload: dict[str, Any]) -> bool:
        raw_status = str(payload.get("status", "UNKNOWN")).upper()
        ticket = _positive_int(payload.get("ticket")) or 0
        position_ticket = _positive_int(payload.get("position_ticket"))
        entry_price = _positive_float(payload.get("entry_price"))
        lot_size = _positive_float(payload.get("lot_size"))
        executed_open = raw_status == "EXECUTED" and ticket > 0
        rejected = raw_status.startswith("REJECTED")
        status = "EXECUTED_OPEN" if executed_open else ("REJECTED" if rejected else raw_status)
        reason_code = raw_status

        with self._lock:
            rows = [row for row in self.trades if row["trade_id"] == payload.get("trade_id")]
            for row in rows:
                if executed_open:
                    row.update({
                        "trade_ticket": ticket,
                        "position_ticket": position_ticket or ticket,
                        "status": status,
                        "lot_size": lot_size if lot_size is not None else row["lot_size"],
                        "entry_price": entry_price if entry_price is not None else row["entry_price"],
                        "slippage": payload.get("slippage", 0.0),
                        "spread_exec_price": payload.get("spread_at_entry", 0.0),
                        "execution_time": payload.get("execution_time") or payload.get("close_time") or _now(),
                    })
                else:
                    row.update({
                        "trade_ticket": ticket if ticket > 0 else None,
                        "position_ticket": row["position_ticket"] if row["position_ticket"] is not None
                        else (position_ticket or ticket),
                        "status": status,
                        "reason_code": reason_code,
                        "entry_price": payload.get("entry_price", 0.0),
                        "slippage": payload.get("slippage", 0.0),
                        "spread_exec_price": payload.get("spread_at_entry", 0.0),
                        "close_time": payload.get("close_time", _now()),
                    })
            if rows:
                self._record_lifecycle(str(payload.get("trade_id", "")), "EXECUTION", status, reason_code, payload)
        return bool(rows)

    def mark_trade_execution_uncertain(
        self,
        trade_id: str,
        reason_code: str = "PRESERVE_10_EXECUTION_UNCERTAIN",
    ) -> None:
        with self._lock:
            rows = [row for row in self.trades
                    if row["trade_id"] == trade_id and row["status"] in ("PENDING", "EXECUTION_UNCERTAIN")]
            if not rows:
                return
            for row in rows:
                row.update({"status": "EXECUTION_UNCERTAIN", "reason_code": reason_code})
            for reservation in self.trade_reservations:
                if reservation["trade_id"] == trade_id and reservation["state"] in ("RESERVED", "ROUTED"):
                    reservation.update({"state": "EXECUTION_UNCERTAIN", "reason_code": reason_code,
                                        "updated_at": _now()})
            self._record_lifecycle(trade_id, "EXECUTION_UNCERTAIN", "EXECUTION_UNCERTAIN", reason_code)

    def update_trade_exit_result(
        self,
        payload: dict[str, Any],
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> bool:
        if payload.get("is_final_exit") is False:
            return False

        pnl = float(payload.get("profit_loss", 0.0))
        raw_status = str(payload.get("status", "CLOSED")).upper()
        if raw_status.startswith("CLOSED"):
            status = raw_status
        elif pnl > 0:
            status = "CLOSED_WIN"
        elif pnl < 0:
            status = "CLOSED_LOSS"
        else:
            status = "CLOSED_BREAKEVEN"

        close_time = payload.get("close_time", _now())
        r_multiple = payload.get("r_multiple")
        position_ticket = _positive_int(payload.get("position_ticket"))
        ticket = _positive_int(payload.get("ticket"))
        trade_id = str(payload.get("trade_id") or "").strip() or None

        def _apply_update(column: str, match_value: Any) -> int:
            matched = [
                row for row in self.trades
                if row[column] == match_value and (
                    evidence_context is None
                    or (row["evidence_stream"], row["account_scope"])
                    == (evidence_context.evidence_stream, evidence_context.account_scope)
                )
            ]
            for row in matched:
                row.update({"status": status, "profit_loss": pnl, "close_time": close_time})
                if r_multiple is not None:
                    row["r_multiple"] = float(r_multiple)
            return len(matched)

        with self._lock:
            updated = 0
            if position_ticket is not None:
                updated = _apply_update("position_ticket", position_ticket)
            if updated == 0 and trade_id:
                updated = _apply_update("trade_id", trade_id)
            if updated == 0 and ticket is not None:
                updated = _apply_update("trade_ticket", ticket)
            if updated > 0 and trade_id:
                self._record_lifecycle(trade_id, "EXIT", status, raw_status, payload)
        return updated > 0

    def get_trade_by_identifiers(
        self,
        *,
        trade_id: str | None = None,
        position_ticket: int | None = None,
        trade_ticket: int | None = None,
        evidence_context: EvidenceContext | None = None,
    ) -> dict[str, Any] | None:
        # The SQL joins every supplied clause with OR (evidence clauses included);
        # kept identical here. A NULL parameter never matches.
        clauses: list[tuple[str, Any]] = []
        if position_ticket is not None:
            clauses.append(("position_ticket", _positive_int(position_ticket)))
        if trade_ticket is not None:
            clauses.append(("trade_ticket", _positive_int(trade_ticket)))
        if trade_id:
            clauses.append(("trade_id", str(trade_id)))
        if evidence_context is not None:
            clauses.append(("evidence_stream", evidence_context.evidence_stream))
            clauses.append(("account_scope", evidence_context.account_scope))
        if not clauses:
            return None
        with self._lock:
            for row in reversed(self.trades):
                if any(value is not None and row[column] == value for column, value in clauses):
                    return {key: row[key] for key in (
                        "trade_id", "symbol", "direction", "stop_loss", "take_profit", "lot_size",
                        "entry_price", "r_multiple", "status")}
        return None

    def get_open_trade_ledger(
        self,
        *,
        evidence_stream: str | None = None,
        account_scope: str | None = None,
    ) -> dict[str, Any]:
        rows = self.list_open_trades(evidence_stream=evidence_stream, account_scope=account_scope)
        return {
            "open_trade_count": len(rows),
            "open_risk_percent": round(sum(float(row["risk_percent"] or 0.0) for row in rows), 8),
            "open_symbols": sorted({str(row["symbol"]) for row in rows if row["symbol"]}),
            "open_trade_ids": sorted(str(row["trade_id"]) for row in rows if row["trade_id"]),
            "open_trade_tickets": sorted(
                {int(row["trade_ticket"]) for row in rows if _positive_int(row["trade_ticket"]) is not None}
            ),
            "open_position_tickets": sorted(
                {int(row["position_ticket"]) for row in rows if _positive_int(row["position_ticket"]) is not None}
            ),
            "open_statuses": sorted({str(row["status"]) for row in rows if row["status"]}),
        }

    def list_open_trades(
        self,
        *,
        evidence_stream: str | None = None,
        account_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        def _or_zero(value: Any) -> Any:
            return 0.0 if value is None else value

        with self._lock:
            return [
                {
                    "trade_id": row["trade_id"],
                    "trade_ticket": row["trade_ticket"],
                    "position_ticket": row["position_ticket"],
                    "symbol": row["symbol"],
                    "direction": row["direction"],
                    "status": row["status"],
                    "risk_percent": _or_zero(row["risk_percent"]),
                    "stop_loss": _or_zero(row["stop_loss"]),
                    "take_profit": _or_zero(row["take_profit"]),
                    "entry_price": _or_zero(row["entry_price"]),
                    "lot_size": _or_zero(row["lot_size"]),
                    "open_time": row["open_time"],
                    "execution_time": row["execution_time"],
                }
                for row in self.trades
                if row["status"] in ("EXECUTED_OPEN", "EXECUTION_UNCERTAIN")
                and (not evidence_stream or row["evidence_stream"] == evidence_stream)
                and (not account_scope or row["account_scope"] == account_scope)
            ]

    def feedback_receipt_exists(
        self,
        feedback_kind: str,
        dedupe_key: str,
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> bool:
        evidence = evidence_context or _default_evidence_context()
        with self._lock:
            return self._receipt(evidence, feedback_kind, dedupe_key) is not None

    def record_feedback_receipt(
        self,
        feedback_kind: str,
        dedupe_key: str,
        *,
        trade_id: str | None = None,
        ticket: int | None = None,
        status: str | None = None,
        close_time: str | None = None,
        source_file: str | None = None,
        evidence_context: EvidenceContext | None = None,
    ) -> bool:
        """Persist a feedback receipt. Returns True when inserted, False on duplicate."""
        evidence = evidence_context or _default_evidence_context()
        with self._lock:
            existing = self._receipt(evidence, feedback_kind, dedupe_key)
            if existing is not None:
                existing["timestamp"] = _now()
                return False
            self.feedback_receipts.append({
                "id": next(self._ids),
                "timestamp": _now(),
                **self._evidence_columns(evidence),
                "feedback_kind": feedback_kind,
                "dedupe_key": dedupe_key,
                "trade_id": trade_id,
                "ticket": _positive_int(ticket),
                "status": status,
                "close_time": close_time,
                "source_file": source_file,
            })
            return True

    def insert_decision_funnel_event(
        self,
        *,
        decision_time: datetime,
        stage: str,
        outcome: str,
        reason_code: str,
        symbol: str | None = None,
        details: str = "",
        trade_id: str | None = None,
        evidence_context: EvidenceContext | None = None,
    ) -> None:
        evidence = evidence_context or _default_evidence_context()
        with self._lock:
            self.decision_funnel_events.append({
                "id": next(self._ids),
                "timestamp": _now(),
                **self._evidence_columns(evidence),
                "decision_time": decision_time.isoformat(),
                "symbol": symbol,
                "stage": stage,
                "outcome": outcome,
                "reason_code": reason_code,
                "details": details,
                "trade_id": trade_id,
            })

    def insert_ml_shadow_event(
        self,
        *,
        decision_time: datetime,
        symbol: str,
        stage: str,
        primary_gate_outcome: str,
        shadow_outcome: str,
        reason_code: str,
        details: str = "",
        trade_id: str | None = None,
        probability: float | None = None,
        threshold: float | None = None,
        model_loaded: bool = False,
        checkpoint_path: str | None = None,
        feature_schema_version: str | None = None,
        evidence_context: EvidenceContext | None = None,
    ) -> None:
        evidence = evidence_context or _default_evidence_context()
        with self._lock:
            self.ml_shadow_events.append({
                "id": next(self._ids),
                "timestamp": _now(),
                **self._evidence_columns(evidence),
                "decision_time": decision_time.isoformat(),
                "symbol": symbol,
                "trade_id": trade_id,
                "stage": stage,
                "primary_gate_outcome": primary_gate_outcome,
                "shadow_outcome": shadow_outcome,
                "reason_code": reason_code,
                "details": details,
                "probability": probability,
                "threshold": threshold,
                "model_loaded": int(bool(model_loaded)),
                "checkpoint_path": checkpoint_path,
                "feature_schema_version": feature_schema_version,
            })

    # Risk ---------------------------------------------------------------------
    def try_create_trade_reservation(
        self,
        *,
        reservation_id: str,
        trade_id: str,
        symbol: str,
        risk_percent: float,
        policy_hash: str,
        expires_at: str,
        max_positions: int,
        max_combined_exposure: float,
        broker_open_positions: int,
        broker_open_risk: float,
    ) -> dict[str, Any]:
        now = _now()
        if not reservation_id or not trade_id or risk_percent <= 0:
            return {"approved": False, "reason_code": "RESERVATION_INPUT_INVALID"}
        with self._lock:
            if self._reservation(trade_id=trade_id, reservation_id=reservation_id) is not None:
                return {"approved": False, "reason_code": "RESERVATION_DUPLICATE"}
            active = [row for row in self.trade_reservations if row["state"] in _ACTIVE_RESERVATION_STATES]
            reserved_count = len(active)
            reserved_risk = float(sum(float(row["risk_percent"]) for row in active))
            if int(broker_open_positions) + reserved_count >= int(max_positions):
                return {"approved": False, "reason_code": "RESERVATION_MAX_POSITIONS"}
            projected_risk = float(broker_open_risk) + reserved_risk + float(risk_percent)
            if projected_risk > float(max_combined_exposure) + 1e-12:
                return {"approved": False, "reason_code": "RESERVATION_MAX_EXPOSURE"}
            self.trade_reservations.append({
                "reservation_id": reservation_id,
                "trade_id": trade_id,
                "symbol": symbol,
                "risk_percent": float(risk_percent),
                "policy_hash": policy_hash,
                "state": "RESERVED",
                "reason_code": "RESERVATION_APPROVED",
                "created_at": now,
                "updated_at": now,
                "expires_at": expires_at,
            })
        return {
            "approved": True,
            "reason_code": "RESERVATION_APPROVED",
            "reservation_id": reservation_id,
            "projected_risk": projected_risk,
        }

    def _transition(self, column: str, value: str, state: str, reason_code: str, allowed: set[str]) -> bool:
        normalized = str(state).upper()
        if normalized not in allowed:
            raise ValueError(f"Unsupported reservation state={state!r}")
        with self._lock:
            matched = [row for row in self.trade_reservations
                       if row[column] == value and row["state"] in _RESERVATION_PREDECESSORS[normalized]]
            for row in matched:
                row.update({"state": normalized, "reason_code": reason_code, "updated_at": _now()})
        return bool(matched)

    def transition_trade_reservation(
        self,
        reservation_id: str,
        state: str,
        reason_code: str,
    ) -> bool:
        allowed = {"ROUTED", "EXECUTION_UNCERTAIN", "EXECUTED", "REJECTED", "RELEASED", "EXPIRED", "CLOSED"}
        return self._transition("reservation_id", reservation_id, state, reason_code, allowed)

    def transition_trade_reservation_by_trade_id(
        self,
        trade_id: str,
        state: str,
        reason_code: str,
    ) -> bool:
        allowed = {"EXECUTION_UNCERTAIN", "EXECUTED", "REJECTED", "RELEASED", "EXPIRED", "CLOSED"}
        return self._transition("trade_id", trade_id, state, reason_code, allowed)

    def insert_account_metrics(
        self,
        account_status: AccountStatus,
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> None:
        evidence = evidence_context or _default_evidence_context()
        with self._lock:
            self.account_metrics.append({
                "id": next(self._ids),
                **self._evidence_columns(evidence),
                "timestamp": account_status.updated_at.isoformat(),
                "balance": account_status.balance,
                "equity": account_status.equity,
                "open_risk_percent": account_status.open_risk_percent,
                "open_usd_exposure_count": account_status.open_usd_exposure_count,
                "daily_loss_percent": account_status.daily_loss_percent,
                "weekly_loss_percent": account_status.weekly_loss_percent,
                "drawdown_percent": account_status.drawdown_percent,
                "peak_equity": account_status.peak_equity,
                "daily_anchor_date": account_status.daily_anchor_date,
                "daily_anchor_equity": account_status.daily_anchor_equity,
                "weekly_anchor_key": account_status.weekly_anchor_key,
                "weekly_anchor_equity": account_status.weekly_anchor_equity,
                "consecutive_losses": account_status.consecutive_losses,
                "is_trading_halted": int(account_status.is_trading_halted),
            })

    def get_latest_account_metric(
        self,
        *,
        evidence_stream: str | None = None,
        account_scope: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            rows = [
                row for row in self.account_metrics
                if (not evidence_stream or row["evidence_stream"] == evidence_stream)
                and (not account_scope or row["account_scope"] == account_scope)
            ]
            if not rows:
                return None
            latest = max(rows, key=lambda row: (row["timestamp"], row["id"]))
            return {key: latest[key] for key in _ACCOUNT_METRIC_COLUMNS}

    def insert_risk_event(
        self,
        rule_name: str,
        severity: str,
        reason: str,
        trade_id: str | None = None,
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> None:
        evidence = evidence_context or _default_evidence_context()
        with self._lock:
            self.risk_events.append({
                "id": next(self._ids),
                "timestamp": _now(),
                **self._evidence_columns(evidence),
                "rule_name": rule_name,
                "severity": severity,
                "reason": reason,
                "trade_id": trade_id,
            })

    # Runtime state --------------------------------------------------------------
    def get_engine_runtime_state(self, key: str, *, evidence_context: EvidenceContext) -> str | None:
        with self._lock:
            row = self.engine_runtime_state.get(
                (evidence_context.evidence_stream, evidence_context.account_scope, key)
            )
        return None if row is None else str(row["value"])

    def set_engine_runtime_state(self, key: str, value: str, *, evidence_context: EvidenceContext) -> None:
        with self._lock:
            self.engine_runtime_state[(evidence_context.evidence_stream, evidence_context.account_scope, key)] = {
                "value": str(value),
                "updated_at": _now(),
            }
