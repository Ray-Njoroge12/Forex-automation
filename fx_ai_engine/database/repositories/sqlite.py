"""SQLite implementation of ``application.repository.EngineRepository``.

Each method forwards to the repository-module function of the same name,
looked up at call time, so patching ``database.connection.get_conn`` /
``DB_PATH`` or a function in ``database.repositories.{trades,risk,config}``
still takes effect. No behaviour of its own.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.account_status import AccountStatus
from core.evidence import EvidenceContext
from core.types import TechnicalSignal
from database.repositories import config as _config
from database.repositories import risk as _risk
from database.repositories import trades as _trades


class SqliteEngineRepository:
    # Trades -----------------------------------------------------------------
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
        _trades.insert_trade_proposal(
            signal,
            status,
            reason_code,
            risk_percent,
            market_regime,
            evidence_context=evidence_context,
            regime_confidence=regime_confidence,
            atr_ratio=atr_ratio,
            is_london_session=is_london_session,
            is_newyork_session=is_newyork_session,
            rate_differential=rate_differential,
        )

    def apply_execution_feedback(self, payload: dict[str, Any], *, dedupe_key: str, source_file: str,
                                 evidence_context: EvidenceContext) -> str:
        return _trades.apply_execution_feedback(
            payload, dedupe_key=dedupe_key, source_file=source_file, evidence_context=evidence_context
        )

    def has_unresolved_execution_feedback(self, evidence_context: EvidenceContext) -> bool:
        return _trades.has_unresolved_execution_feedback(evidence_context)

    def update_trade_execution_result(self, payload: dict[str, Any]) -> bool:
        return _trades.update_trade_execution_result(payload)

    def mark_trade_execution_uncertain(
        self,
        trade_id: str,
        reason_code: str = "PRESERVE_10_EXECUTION_UNCERTAIN",
    ) -> None:
        _trades.mark_trade_execution_uncertain(trade_id, reason_code)

    def update_trade_exit_result(
        self,
        payload: dict[str, Any],
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> bool:
        return _trades.update_trade_exit_result(payload, evidence_context=evidence_context)

    def get_trade_by_identifiers(
        self,
        *,
        trade_id: str | None = None,
        position_ticket: int | None = None,
        trade_ticket: int | None = None,
        evidence_context: EvidenceContext | None = None,
    ) -> dict[str, Any] | None:
        return _trades.get_trade_by_identifiers(
            trade_id=trade_id,
            position_ticket=position_ticket,
            trade_ticket=trade_ticket,
            evidence_context=evidence_context,
        )

    def get_open_trade_ledger(
        self,
        *,
        evidence_stream: str | None = None,
        account_scope: str | None = None,
    ) -> dict[str, Any]:
        return _trades.get_open_trade_ledger(evidence_stream=evidence_stream, account_scope=account_scope)

    def list_open_trades(
        self,
        *,
        evidence_stream: str | None = None,
        account_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        return _trades.list_open_trades(evidence_stream=evidence_stream, account_scope=account_scope)

    def feedback_receipt_exists(
        self,
        feedback_kind: str,
        dedupe_key: str,
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> bool:
        return _trades.feedback_receipt_exists(feedback_kind, dedupe_key, evidence_context=evidence_context)

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
        return _trades.record_feedback_receipt(
            feedback_kind,
            dedupe_key,
            trade_id=trade_id,
            ticket=ticket,
            status=status,
            close_time=close_time,
            source_file=source_file,
            evidence_context=evidence_context,
        )

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
        _trades.insert_decision_funnel_event(
            decision_time=decision_time,
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
            symbol=symbol,
            details=details,
            trade_id=trade_id,
            evidence_context=evidence_context,
        )

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
        _trades.insert_ml_shadow_event(
            decision_time=decision_time,
            symbol=symbol,
            stage=stage,
            primary_gate_outcome=primary_gate_outcome,
            shadow_outcome=shadow_outcome,
            reason_code=reason_code,
            details=details,
            trade_id=trade_id,
            probability=probability,
            threshold=threshold,
            model_loaded=model_loaded,
            checkpoint_path=checkpoint_path,
            feature_schema_version=feature_schema_version,
            evidence_context=evidence_context,
        )

    # Risk -------------------------------------------------------------------
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
        return _risk.try_create_trade_reservation(
            reservation_id=reservation_id,
            trade_id=trade_id,
            symbol=symbol,
            risk_percent=risk_percent,
            policy_hash=policy_hash,
            expires_at=expires_at,
            max_positions=max_positions,
            max_combined_exposure=max_combined_exposure,
            broker_open_positions=broker_open_positions,
            broker_open_risk=broker_open_risk,
        )

    def transition_trade_reservation(
        self,
        reservation_id: str,
        state: str,
        reason_code: str,
    ) -> bool:
        return _risk.transition_trade_reservation(reservation_id, state, reason_code)

    def transition_trade_reservation_by_trade_id(
        self,
        trade_id: str,
        state: str,
        reason_code: str,
    ) -> bool:
        return _risk.transition_trade_reservation_by_trade_id(trade_id, state, reason_code)

    def insert_account_metrics(
        self,
        account_status: AccountStatus,
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> None:
        _risk.insert_account_metrics(account_status, evidence_context=evidence_context)

    def get_latest_account_metric(
        self,
        *,
        evidence_stream: str | None = None,
        account_scope: str | None = None,
    ) -> dict[str, Any] | None:
        return _risk.get_latest_account_metric(evidence_stream=evidence_stream, account_scope=account_scope)

    def insert_risk_event(
        self,
        rule_name: str,
        severity: str,
        reason: str,
        trade_id: str | None = None,
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> None:
        _risk.insert_risk_event(rule_name, severity, reason, trade_id, evidence_context=evidence_context)

    # Runtime state ------------------------------------------------------------
    def get_engine_runtime_state(self, key: str, *, evidence_context: EvidenceContext) -> str | None:
        return _config.get_engine_runtime_state(key, evidence_context=evidence_context)

    def set_engine_runtime_state(self, key: str, value: str, *, evidence_context: EvidenceContext) -> None:
        _config.set_engine_runtime_state(key, value, evidence_context=evidence_context)
