"""Persistence interfaces the application layer depends on (August audit §2).

Application services call these methods on an injected repository instead of
importing ``database.db``. The composition root (``main.py``) chooses the
implementation:

- ``database.repositories.sqlite.SqliteEngineRepository``: the live engine;
  every method forwards to the ``database.repositories`` function of the same
  name, so behaviour is unchanged.
- ``database.repositories.memory.InMemoryEngineRepository``: replay and unit
  tests; same results, no SQLite file.

Method names and signatures match the ``database.db`` functions they stand for
(``tests/test_contract_repository.py`` checks this and runs the same behaviour
contract against both implementations).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from core.account_status import AccountStatus
from core.evidence import EvidenceContext
from core.types import TechnicalSignal


@runtime_checkable
class TradeRepository(Protocol):
    """Proposals, execution and exit feedback, receipts and decision records."""

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
    ) -> None: ...

    def apply_execution_feedback(self, payload: dict[str, Any], *, dedupe_key: str, source_file: str,
                                 evidence_context: EvidenceContext) -> str: ...

    def has_unresolved_execution_feedback(self, evidence_context: EvidenceContext) -> bool: ...

    def update_trade_execution_result(self, payload: dict[str, Any]) -> bool: ...

    def mark_trade_execution_uncertain(
        self,
        trade_id: str,
        reason_code: str = "PRESERVE_10_EXECUTION_UNCERTAIN",
    ) -> None: ...

    def update_trade_exit_result(
        self,
        payload: dict[str, Any],
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> bool: ...

    def get_trade_by_identifiers(
        self,
        *,
        trade_id: str | None = None,
        position_ticket: int | None = None,
        trade_ticket: int | None = None,
        evidence_context: EvidenceContext | None = None,
    ) -> dict[str, Any] | None: ...

    def get_open_trade_ledger(
        self,
        *,
        evidence_stream: str | None = None,
        account_scope: str | None = None,
    ) -> dict[str, Any]: ...

    def list_open_trades(
        self,
        *,
        evidence_stream: str | None = None,
        account_scope: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def feedback_receipt_exists(
        self,
        feedback_kind: str,
        dedupe_key: str,
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> bool: ...

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
    ) -> bool: ...

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
    ) -> None: ...

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
    ) -> None: ...


@runtime_checkable
class RiskRepository(Protocol):
    """Reservations, account metrics and risk events."""

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
    ) -> dict[str, Any]: ...

    def transition_trade_reservation(
        self,
        reservation_id: str,
        state: str,
        reason_code: str,
    ) -> bool: ...

    def transition_trade_reservation_by_trade_id(
        self,
        trade_id: str,
        state: str,
        reason_code: str,
    ) -> bool: ...

    def insert_account_metrics(
        self,
        account_status: AccountStatus,
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> None: ...

    def get_latest_account_metric(
        self,
        *,
        evidence_stream: str | None = None,
        account_scope: str | None = None,
    ) -> dict[str, Any] | None: ...

    def insert_risk_event(
        self,
        rule_name: str,
        severity: str,
        reason: str,
        trade_id: str | None = None,
        *,
        evidence_context: EvidenceContext | None = None,
    ) -> None: ...


@runtime_checkable
class RuntimeStateRepository(Protocol):
    """Engine settings that survive a restart (``database/repositories/config.py``)."""

    def get_engine_runtime_state(self, key: str, *, evidence_context: EvidenceContext) -> str | None: ...

    def set_engine_runtime_state(self, key: str, value: str, *, evidence_context: EvidenceContext) -> None: ...


@runtime_checkable
class EngineRepository(TradeRepository, RiskRepository, RuntimeStateRepository, Protocol):
    """Everything the engine's application services persist or read."""
