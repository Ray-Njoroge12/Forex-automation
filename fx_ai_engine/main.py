from __future__ import annotations

import argparse
import json
import logging
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from config_microcapital import (
    ALLOW_NON_SRS_POLICY_ENV,
    MODE_CONFIGS,
    apply_runtime_experiment_config,
    collect_runtime_override_audit,
    get_policy_config,
    is_non_srs_policy_mode,
    non_srs_policy_allowed,
    read_predict_threshold,
    runtime_overrides_enabled,
)
from bridge.execution_feedback import ExecutionFeedbackReader
from bridge.mock_feedback_simulator import MockFeedbackSimulator
from bridge.signal_router import SignalRouter
from application.repository import EngineRepository
from application.routing_service import RoutingService
from core.account_status import AccountStatus
from core.bridge_utils import get_mt5_bridge_path
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.credentials import CredentialsError, load_mt5_credentials_from_env
from core.evidence import build_runtime_evidence_context
from core.env_loader import load_runtime_env
from core.sentiment.sentiment_agent import SentimentAgent
from core.strategy import (
    DEFAULT_STRATEGY_ID,
    StrategyNotLiveEligibleError,
    build_strategy,
    require_live_eligible,
)
from ml.signal_ranker import SignalRanker
from ml.meta_labeler.shadow_runtime import (
    MetaLabelerShadowRuntime,
    resolve_canary_runtime_config,
)
from core.logging_utils import configure_logging

load_runtime_env()
from core.alerts import alert_risk_halt
from core.health import start_health_api
from core import live_gate
from core.metrics import init_metrics
from core.mt5_bridge import MT5Connection
from core.file_bridge_connection import FileBridgeConnection
from core.observability import init_tracing
from core.risk.hard_risk_engine import HardRiskEngine
from core.risk.reset_scheduler import ResetScheduler
from database.db import (
    initialize_schema,
    migrate_add_decision_funnel_events,
    migrate_add_evidence_partition_columns,
    migrate_add_feedback_receipts,
    migrate_add_ml_feature_columns,
    migrate_add_ml_shadow_events,
    migrate_add_risk_events,
    migrate_add_restart_state_columns,
    migrate_add_trade_reservations,
    migrate_add_trade_lifecycle_events,
    migrate_mark_stale_trades,
    migrate_phase8_columns,
    run_all_migrations,
)
from database.repositories.sqlite import SqliteEngineRepository
# Engine responsibilities live in application/* (Phase 4 split). main.py is the
# composition root: start-up approvals, Engine wiring and one-line delegates.
# Names that moved are re-exported here so existing imports keep working.
from application import decision_cycle, engine_runtime, feedback_service, host_tolerance, market_data_service
from application.decision_cycle import (  # noqa: F401
    _format_pre_route_feasibility_event,
    _format_strategic_risk_event,
    _normalize_pre_route_feasibility_details,
    _policy_evidence_suffix,
    _with_policy_evidence,
)
from application.engine_runtime import _demo_account_state, _demo_account_verified  # noqa: F401
from application.feedback_service import (  # noqa: F401
    _estimate_recovered_r_multiple,
    _execution_feedback_dedupe_key,
    _exit_feedback_dedupe_key,
    _normalise_legacy_exit_close_time,
    _parse_timestamp_utc,
    _pip_size,
    _safe_int,
)
from application.market_data_service import (  # noqa: F401
    LAST_M15_CANDLE_STATE_KEY,
    MAX_ACCOUNT_SNAPSHOT_AGE_SECONDS,
    SYMBOLS,
    _live_trade_mgmt_option_c_enabled,
    _snapshot_age_seconds,
)

logger = logging.getLogger("fx_ai_engine.main")

# The only strategy the live/demo engine runs (ADR 0014). No CLI flag and no
# environment variable: running another strategy live is an owner/SRS decision.
LIVE_STRATEGY_ID = DEFAULT_STRATEGY_ID

# HT-009A-1: a loss-streak count that could not go into an account metric yet.
PENDING_LOSS_STREAK_STATE_KEY = "pending_loss_streak"


def _strict_non_negative_int(value: object) -> int | None:
    """A whole number >= 0; None for booleans, fractions, strings and anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _parse_pending_loss_streak(raw: str) -> tuple[int | None, datetime | None]:
    """(count, recorded_at) from the pending state; count None when malformed."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return _strict_non_negative_int(payload.get("consecutive_losses")), _parse_utc_timestamp(payload.get("recorded_at"))

PRESERVE_10_COMMISSION_PER_LOT_ENV = "PRESERVE_10_COMMISSION_PER_LOT_USD"
PRESERVE_10_LOT_SIZE = 0.01
PRESERVE_10_REFERENCE_BALANCE_USD = 10.0


def _predict_threshold() -> float:
    return read_predict_threshold()


@dataclass(frozen=True)
class Preserve10StartupApprovalDecision:
    approved: bool
    reason_code: str
    details: str


@dataclass(frozen=True)
class RuntimePolicyApprovalDecision:
    approved: bool
    reason_code: str
    details: str


def _startup_approve(reason_code: str, details: str) -> Preserve10StartupApprovalDecision:
    return Preserve10StartupApprovalDecision(True, reason_code, details)


def _startup_reject(reason_code: str, details: str) -> Preserve10StartupApprovalDecision:
    return Preserve10StartupApprovalDecision(False, reason_code, details)


def _policy_runtime_approve(reason_code: str, details: str) -> RuntimePolicyApprovalDecision:
    return RuntimePolicyApprovalDecision(True, reason_code, details)


def _policy_runtime_reject(reason_code: str, details: str) -> RuntimePolicyApprovalDecision:
    return RuntimePolicyApprovalDecision(False, reason_code, details)


def evaluate_runtime_policy_approval(
    bridge: MT5Connection,
    *,
    policy: dict | None = None,
    use_mock: bool,
    env: Mapping[str, str] | None = None,
) -> RuntimePolicyApprovalDecision:
    runtime_policy = get_policy_config() if policy is None else dict(policy)
    mode_id = str(runtime_policy.get("MODE_ID") or "").strip().lower()
    if not is_non_srs_policy_mode(mode_id):
        return _policy_runtime_approve(
            "CORE_SRS_RUNTIME_APPROVED",
            _with_policy_evidence(runtime_policy, "Core SRS runtime policy approved"),
        )
    if live_gate.live_session_covers(runtime_policy, bridge):
        return _policy_runtime_approve(
            "MICRO_LIVE_LIVE_GATE_APPROVED",
            _with_policy_evidence(runtime_policy, "Non-SRS policy 'micro_live' approved by the live gate for this session"),
        )

    source = os.environ if env is None else env
    if not non_srs_policy_allowed(source):
        return _policy_runtime_reject(
            "NON_SRS_POLICY_EXPLICIT_APPROVAL_REQUIRED",
            _with_policy_evidence(
                runtime_policy,
                (
                    f"Non-SRS runtime policy '{mode_id}' is blocked because {ALLOW_NON_SRS_POLICY_ENV}=1 "
                    "is required for research-only runtime modes."
                ),
            ),
        )

    if use_mock:
        return _policy_runtime_approve(
            "NON_SRS_POLICY_RESEARCH_APPROVED",
            _with_policy_evidence(
                runtime_policy,
                (
                    f"Non-SRS runtime policy '{mode_id}' approved for research because USE_MT5_MOCK=1 "
                    f"and {ALLOW_NON_SRS_POLICY_ENV}=1."
                ),
            ),
        )

    account_is_demo: bool | None
    try:
        account_is_demo = bridge.is_demo_account()
    except Exception:
        account_is_demo = None
    if account_is_demo is True:
        return _policy_runtime_approve(
            "NON_SRS_POLICY_RESEARCH_APPROVED",
            _with_policy_evidence(
                runtime_policy,
                (
                    f"Non-SRS runtime policy '{mode_id}' approved for research on a verified demo account "
                    f"because {ALLOW_NON_SRS_POLICY_ENV}=1."
                ),
            ),
        )

    account_status = "live" if account_is_demo is False else "unverified"
    return _policy_runtime_reject(
        "NON_SRS_POLICY_REQUIRES_MOCK_OR_DEMO",
        _with_policy_evidence(
            runtime_policy,
            (
                f"Non-SRS runtime policy '{mode_id}' is blocked because research-only runtime modes require "
                f"USE_MT5_MOCK=1 or a verified demo account (account_status={account_status})."
            ),
        ),
    )


def _insert_risk_event_with_context(
    rule_name: str,
    severity: str,
    reason: str,
    trade_id: str | None = None,
    *,
    evidence_context=None,
    repository: EngineRepository,
    alert: bool = True,
) -> None:
    if alert and severity in ("BLOCK", "CRITICAL"):
        alert_risk_halt(rule_name, reason, severity)
    try:
        repository.insert_risk_event(
            rule_name,
            severity,
            reason,
            trade_id,
            evidence_context=evidence_context,
        )
    except TypeError as exc:
        if "evidence_context" not in str(exc):
            raise
        repository.insert_risk_event(rule_name, severity, reason, trade_id)


def _insert_funnel_event_with_context(
    *,
    decision_time: datetime,
    stage: str,
    outcome: str,
    reason_code: str,
    symbol: str | None = None,
    details: str = "",
    trade_id: str | None = None,
    evidence_context=None,
    repository: EngineRepository,
) -> None:
    try:
        repository.insert_decision_funnel_event(
            decision_time=decision_time,
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
            symbol=symbol,
            details=details,
            trade_id=trade_id,
            evidence_context=evidence_context,
        )
    except TypeError as exc:
        if "evidence_context" not in str(exc):
            raise
        repository.insert_decision_funnel_event(
            decision_time=decision_time,
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
            symbol=symbol,
            details=details,
            trade_id=trade_id,
        )


def _read_preserve_10_commission_per_lot_usd(
    env: Mapping[str, str] | None = None,
) -> tuple[float | None, str | None]:
    source = os.environ if env is None else env
    raw = source.get(PRESERVE_10_COMMISSION_PER_LOT_ENV, "").strip()
    if raw == "":
        return None, None

    try:
        commission = float(raw)
    except ValueError:
        return None, f"invalid {PRESERVE_10_COMMISSION_PER_LOT_ENV}={raw!r}"

    if commission < 0:
        return None, f"negative {PRESERVE_10_COMMISSION_PER_LOT_ENV}={raw!r}"
    return commission, None


def _enforce_demo_only_experiment_account_gate(
    policy: Mapping[str, object],
    *,
    bridge: MT5Connection,
    use_mock: bool,
) -> dict:
    gated = deepcopy(dict(policy))
    experiments = gated.get("EXPERIMENTS", {})
    if not isinstance(experiments, Mapping):
        return gated

    active = [
        key
        for key, cfg in experiments.items()
        if isinstance(cfg, Mapping) and bool(cfg.get("enabled", False))
    ]
    if not active or use_mock:
        return gated

    account_is_demo: bool | None = None
    if hasattr(bridge, "is_demo_account"):
        try:
            account_is_demo = bridge.is_demo_account()
        except Exception as exc:
            logger.warning("Failed checking MT5 account demo status for experiments: %s", exc)
            account_is_demo = None

    if account_is_demo is True:
        return gated

    for key in active:
        cfg = experiments.get(key)
        if isinstance(cfg, dict):
            cfg["enabled"] = False
    gated.pop("EXPERIMENT_TAG", None)
    account_status = "non-demo" if account_is_demo is False else "unverified"
    logger.warning(
        "Disabled demo-only runtime experiments %s because connected MT5 account is %s",
        ",".join(active),
        account_status,
    )
    return gated


def evaluate_preserve_10_startup_approval(
    bridge: MT5Connection,
    *,
    policy: dict | None = None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> Preserve10StartupApprovalDecision:
    runtime_policy = get_policy_config() if policy is None else dict(policy)
    if runtime_policy["MODE_ID"] != "preserve_10":
        return _startup_approve(
            "PRESERVE_10_STARTUP_GATE_BYPASS",
            _with_policy_evidence(
                runtime_policy,
                "Active runtime mode does not require Preserve-$10 startup approval",
            ),
        )

    facts = bridge.get_preserve_10_approval_facts(now=now)
    if not facts.can_assess:
        return _startup_reject(
            facts.reason_code,
            _with_policy_evidence(
                runtime_policy,
                (
                    "Preserve-$10 startup approval blocked before engine start because required broker/account "
                    f"approval facts could not be assessed (source_reason={facts.reason_code}; source_detail={facts.details}). "
                    "Resolve the MT5/account data issue and retry."
                ),
            ),
        )

    account = facts.account
    if account is None:
        return _startup_reject(
            "PRESERVE_10_ACCOUNT_FACTS_MISSING",
            _with_policy_evidence(
                runtime_policy,
                "Preserve-$10 startup approval blocked before engine start because the approval snapshot did not include account facts.",
            ),
        )

    if not account.trade_allowed:
        return _startup_reject(
            "PRESERVE_10_ACCOUNT_TRADE_DISABLED",
            _with_policy_evidence(
                runtime_policy,
                (
                    "Preserve-$10 startup approval blocked before engine start because account trading is disabled "
                    f"currency={account.currency} leverage={account.leverage}"
                ),
            ),
        )

    if account.normalized_balance_usd <= 0:
        return _startup_reject(
            "PRESERVE_10_BALANCE_INVALID",
            _with_policy_evidence(
                runtime_policy,
                (
                    "Preserve-$10 startup approval blocked before engine start because the normalized balance is not positive "
                    f"normalized_balance_usd={account.normalized_balance_usd:.8f}"
                ),
            ),
        )

    commission_per_lot_usd, commission_error = _read_preserve_10_commission_per_lot_usd(env)
    if commission_error is not None:
        return _startup_reject(
            "PRESERVE_10_COST_EVIDENCE_INVALID",
            _with_policy_evidence(
                runtime_policy,
                (
                    "Preserve-$10 startup approval blocked before engine start because supplemental commission evidence is invalid "
                    f"({commission_error})"
                ),
            ),
        )
    if commission_per_lot_usd is None:
        return _startup_reject(
            "PRESERVE_10_COST_EVIDENCE_UNAVAILABLE",
            _with_policy_evidence(
                runtime_policy,
                (
                    "Preserve-$10 startup approval blocked before engine start because commission/cost evidence is missing from broker facts; "
                    f"set supplemental evidence via {PRESERVE_10_COMMISSION_PER_LOT_ENV} and retry"
                ),
            ),
        )

    target_balance_usd = min(account.normalized_balance_usd, PRESERVE_10_REFERENCE_BALANCE_USD)
    fixed_risk_usd = runtime_policy.get("FIXED_RISK_USD")
    risk_budget_usd = (
        float(fixed_risk_usd)
        if fixed_risk_usd is not None
        else target_balance_usd * float(runtime_policy["BASE_RISK_PCT"])
    )
    lot_size = PRESERVE_10_LOT_SIZE
    tolerance = 1e-9

    for symbol, symbol_facts in facts.symbols.items():
        if not symbol_facts.tradable:
            return _startup_reject(
                "PRESERVE_10_SYMBOL_NOT_TRADABLE",
                _with_policy_evidence(
                    runtime_policy,
                    f"Preserve-$10 startup approval blocked before engine start because symbol={symbol} is not tradable",
                ),
            )

        if symbol_facts.volume_min > lot_size + tolerance:
            return _startup_reject(
                "PRESERVE_10_LOT_SIZE_UNSUPPORTED",
                _with_policy_evidence(
                    runtime_policy,
                    (
                        "Preserve-$10 startup approval blocked before engine start because the current bridge/EA path requires 0.01-lot compatibility "
                        f"symbol={symbol} volume_min={symbol_facts.volume_min:.6f}"
                    ),
                ),
            )

        if symbol_facts.volume_step > lot_size + tolerance:
            return _startup_reject(
                "PRESERVE_10_LOT_STEP_UNSUPPORTED",
                _with_policy_evidence(
                    runtime_policy,
                    (
                        "Preserve-$10 startup approval blocked before engine start because the current bridge/EA path requires 2-decimal lot steps "
                        f"symbol={symbol} volume_step={symbol_facts.volume_step:.6f}"
                    ),
                ),
            )

        step_count = round(lot_size / symbol_facts.volume_step)
        if abs((step_count * symbol_facts.volume_step) - lot_size) > tolerance:
            return _startup_reject(
                "PRESERVE_10_LOT_STEP_UNSUPPORTED",
                _with_policy_evidence(
                    runtime_policy,
                    (
                        "Preserve-$10 startup approval blocked before engine start because 0.01 lots are not aligned to broker lot steps "
                        f"symbol={symbol} volume_step={symbol_facts.volume_step:.6f}"
                    ),
                ),
            )

        pip_cost_usd = (
            account.unit_scale
            * symbol_facts.tick_value
            * lot_size
            * (_pip_size(symbol) / symbol_facts.tick_size)
        )
        if pip_cost_usd > 0.01 + tolerance:
            return _startup_reject(
                "PRESERVE_10_ECONOMICS_TOO_COARSE",
                _with_policy_evidence(
                    runtime_policy,
                    (
                        "Preserve-$10 startup approval blocked before engine start because 0.01 lots remain too large in real-dollar terms "
                        f"symbol={symbol} pip_cost_usd={pip_cost_usd:.6f}"
                    ),
                ),
            )

        estimated_margin_usd = (
            account.unit_scale * symbol_facts.min_lot_margin * (lot_size / symbol_facts.volume_min)
        )
        if estimated_margin_usd > target_balance_usd + tolerance:
            return _startup_reject(
                "PRESERVE_10_MARGIN_TOO_HIGH",
                _with_policy_evidence(
                    runtime_policy,
                    (
                        "Preserve-$10 startup approval blocked before engine start because 0.01-lot margin exceeds the preserve-$10 balance floor "
                        f"symbol={symbol} estimated_margin_usd={estimated_margin_usd:.6f} "
                        f"target_balance_usd={target_balance_usd:.6f}"
                    ),
                ),
            )

        spread_cost_usd = pip_cost_usd * symbol_facts.spread_pips
        total_cost_usd = spread_cost_usd + (commission_per_lot_usd * lot_size)
        if total_cost_usd >= risk_budget_usd:
            return _startup_reject(
                "PRESERVE_10_COST_BURDEN_EXCESSIVE",
                _with_policy_evidence(
                    runtime_policy,
                    (
                        "Preserve-$10 startup approval blocked before engine start because startup cost burden is too large for the preserve-$10 risk budget "
                        f"symbol={symbol} total_cost_usd={total_cost_usd:.6f} risk_budget_usd={risk_budget_usd:.6f}"
                    ),
                ),
            )

    return _startup_approve(
        "PRESERVE_10_STARTUP_APPROVED",
        _with_policy_evidence(
            runtime_policy,
            (
                "Preserve-$10 startup approval passed: broker/account facts support 0.01-lot preserve-first operation "
                f"and supplemental commission evidence is present via {PRESERVE_10_COMMISSION_PER_LOT_ENV}"
            ),
        ),
    )


class Engine:
    # Persistence for every engine step (application.repository.EngineRepository).
    # The class-level SQLite default also covers engines built with
    # Engine.__new__ in tests; __init__ and main() inject the instance used.
    repository: EngineRepository = SqliteEngineRepository()
    # Host tolerance (application.host_tolerance). None on engines built with
    # Engine.__new__ in tests; __init__ always sets both (the guard may be disabled).
    host_guard: host_tolerance.HostGuard | None = None
    outage_monitor: host_tolerance.OutageMonitor | None = None
    # HT-009A: True only for the engine main() builds after a refused demo start.
    # It reconciles feedback, exits and broker closures once; it never runs the
    # decision loop or a decision cycle.
    reconcile_only: bool = False
    # HT-009A-1: False in __init__ until the first valid account snapshot is applied.
    # True here so engines built with Engine.__new__ in tests keep the direct checkpoint.
    _account_values_loaded: bool = True
    # HT-009A-1: a loss-streak count kept in engine runtime state until a metric holds it.
    _pending_loss_streak_active: bool = False
    _pending_loss_streak_value: int | None = None
    # HT-009A-1E: True only in a reconciliation-only engine whose pending state was unreadable.
    _loss_streak_unknown: bool = False

    def __init__(
        self,
        bridge: MT5Connection,
        tracer,
        metrics,
        use_mock: bool,
        *,
        run_mode: str,
        policy: Mapping[str, object] | None = None,
        repository: EngineRepository | None = None,
        host_guard: host_tolerance.HostGuard | None = None,
        outage_monitor: host_tolerance.OutageMonitor | None = None,
        reconcile_only: bool = False,
    ):
        # Refuse research-only or unknown strategies before the bridge, the DB,
        # the mock feedback or any agent is touched (owner decision 2026-11-04).
        try:
            require_live_eligible(LIVE_STRATEGY_ID)
        except StrategyNotLiveEligibleError as exc:
            logger.critical("STRATEGY_NOT_LIVE_ELIGIBLE strategy=%s reason=%s", exc.strategy_id, exc.reason)
            raise
        self.bridge = bridge
        self.repository = SqliteEngineRepository() if repository is None else repository
        self.tracer = tracer
        self.metrics = metrics
        self.use_mock = use_mock
        self.run_mode = run_mode
        self.reconcile_only = bool(reconcile_only)
        self.account_status = AccountStatus()
        self.reset_scheduler = ResetScheduler()
        base_policy = dict(policy) if policy is not None else apply_runtime_experiment_config(get_policy_config(), run_mode=run_mode)
        self.policy = _enforce_demo_only_experiment_account_gate(base_policy, bridge=bridge, use_mock=use_mock)
        runtime_policy_approval = evaluate_runtime_policy_approval(
            bridge,
            policy=self.policy,
            use_mock=self.use_mock,
            env=os.environ,
        )
        logger.info(
            "Runtime policy approval: approved=%s reason_code=%s details=%s",
            runtime_policy_approval.approved,
            runtime_policy_approval.reason_code,
            runtime_policy_approval.details,
        )
        if is_non_srs_policy_mode(self.policy["MODE_ID"]) and not runtime_policy_approval.approved and self.reconcile_only:
            logger.warning(
                "Reconciliation-only engine for a blocked non-SRS policy: reason_code=%s",
                runtime_policy_approval.reason_code,
            )
        elif is_non_srs_policy_mode(self.policy["MODE_ID"]) and not runtime_policy_approval.approved:
            raise RuntimeError(
                "Non-SRS runtime policy blocked: "
                f"reason_code={runtime_policy_approval.reason_code} details={runtime_policy_approval.details}"
            )
        preserve_startup_approval = evaluate_preserve_10_startup_approval(
            bridge,
            policy=self.policy,
            env=os.environ,
        )
        logger.info(
            "Preserve-10 startup approval: approved=%s reason_code=%s details=%s",
            preserve_startup_approval.approved,
            preserve_startup_approval.reason_code,
            preserve_startup_approval.details,
        )
        if self.policy["MODE_ID"] == "preserve_10" and not preserve_startup_approval.approved and self.reconcile_only:
            logger.warning(
                "Reconciliation-only engine for a refused Preserve-$10 start: reason_code=%s",
                preserve_startup_approval.reason_code,
            )
        elif self.policy["MODE_ID"] == "preserve_10" and not preserve_startup_approval.approved:
            raise RuntimeError(
                "Preserve-$10 startup approval refused: "
                f"reason_code={preserve_startup_approval.reason_code} details={preserve_startup_approval.details}"
            )
        self.evidence_context = build_runtime_evidence_context(
            self.policy,
            use_mock=use_mock,
            login=getattr(bridge, "login", 0),
            server=getattr(bridge, "server", ""),
        )
        self.predict_threshold = float(self.policy.get("ML_PREDICT_THRESHOLD") or _predict_threshold())
        self._stale_episode_active = False
        self._last_reconciliation_reason = ""
        self._latest_broker_close_time_utc: datetime | None = None
        self._consecutive_losses_dirty = False
        self._consecutive_losses_seeded = False
        self._account_values_loaded = False
        self._pending_loss_streak_active = False
        self._pending_loss_streak_value = None
        self._load_pending_loss_streak()

        # Auto-detect MT5 sandbox bridge path
        self.bridge_path = get_mt5_bridge_path()
        self._validate_bridge_path(self.bridge_path)
        logger.info("Using MT5 bridge path: %s", self.bridge_path)
        self.host_guard = (
            host_tolerance.build_host_guard(use_mock=use_mock, run_mode=run_mode, bridge_path=self.bridge_path)
            if host_guard is None
            else host_guard
        )
        # A BLOCK persisted by an earlier process (or a refused start) is recovered here.
        host_tolerance.restore_guard_state(self.host_guard, self.repository, self.evidence_context)
        self.outage_monitor = (
            host_tolerance.build_outage_monitor(use_mock=use_mock, bridge_path=self.bridge_path, engine_process=True)
            if outage_monitor is None
            else outage_monitor
        )

        self.router = SignalRouter(
            pending_dir=self.bridge_path / "pending_signals",
            lock_dir=self.bridge_path / "active_locks",
            require_database_dedup=True,
        )
        self.routing_service = RoutingService(self.router, self.policy, repository=self.repository)
        self.feedback = ExecutionFeedbackReader(
            feedback_dir=self.bridge_path / "feedback",
            exits_dir=self.bridge_path / "exits",
            allow_mock_artifacts=self.use_mock,
        )
        self.mock_feedback = (
            MockFeedbackSimulator(
                pending_dir=self.bridge_path / "pending_signals",
                feedback_dir=self.bridge_path / "feedback",
                exits_dir=self.bridge_path / "exits",
            )
            if self.use_mock
            else None
        )
        if self.mock_feedback is not None:
            if os.getenv("MT5_MOCK_RESET_STATE") == "1":
                self.mock_feedback.clear_runtime_state()
            self.mock_feedback.clear_account_snapshot()

        # Load static data files once at startup.
        market_data_service.load_market_context(self, os.path.join(os.path.dirname(__file__), "data"))

        # Shared sentiment agent (lazy model load, 15-min cache per symbol).
        # Only active when USE_SENTIMENT=1.
        _sentiment = SentimentAgent()

        self.strategy_id = LIVE_STRATEGY_ID
        self.strategies = {
            sym: build_strategy(
                LIVE_STRATEGY_ID,
                symbol=sym,
                fetch_ohlc=bridge.fetch_ohlc_data,
                fetch_spread=bridge.get_live_spread,
                policy=self.policy,
                run_context="live",
                on_error="reject",
            )
            for sym in SYMBOLS
        }
        self.agents = {
            sym: {
                "adversarial": AdversarialAgent(
                    sym,
                    bridge.fetch_ohlc_data,
                    bridge.get_live_spread,
                    rate_differentials=self._rate_diffs,
                    sentiment_agent=_sentiment,
                ),
            }
            for sym in SYMBOLS
        }
        self.portfolio_manager = PortfolioManager(fetch_ohlc=bridge.fetch_ohlc_data, policy=self.policy)
        self.hard_risk = HardRiskEngine(policy=self.policy)
        self.ranker = SignalRanker()
        self.ranker_loaded = self.ranker.load()
        self.shadow_runtime = MetaLabelerShadowRuntime.from_policy(
            policy=self.policy,
            fetch_ohlc=self.bridge.fetch_ohlc_data,
            env=os.environ,
        )
        self.canary_config = resolve_canary_runtime_config(self.policy, env=os.environ)
        override_audit = collect_runtime_override_audit(self.policy, env=os.environ)
        override_fields = override_audit["fields"]
        logger.info(
            "Runtime override governance: mode=%s allow_non_srs_policy=%s overrides_enabled=%s "
            "fixed_risk={state=%s requested=%s active=%s} "
            "max_spread={state=%s requested=%s active=%s} "
            "ml_threshold={state=%s requested=%s active=%s}",
            override_audit["mode_id"],
            override_audit["allow_non_srs_policy"],
            runtime_overrides_enabled(os.environ),
            override_fields["FIXED_RISK_USD"]["state"],
            override_fields["FIXED_RISK_USD"]["requested"],
            override_fields["FIXED_RISK_USD"]["active"],
            override_fields["MAX_SPREAD_PIPS"]["state"],
            override_fields["MAX_SPREAD_PIPS"]["requested"],
            override_fields["MAX_SPREAD_PIPS"]["active"],
            override_fields["ML_PREDICT_THRESHOLD"]["state"],
            override_fields["ML_PREDICT_THRESHOLD"]["requested"],
            override_fields["ML_PREDICT_THRESHOLD"]["active"],
        )
        logger.info(
            "Runtime config: mode=%s label=%s evidence=%s stream=%s account_scope=%s mock=%s strategy=%s experiment=%s fixed_risk_usd=%s allow_non_srs_policy=%s max_spread_pips=%s ml_threshold=%.3f sentiment=%s ranker_loaded=%s shadow_enabled=%s shadow_model_loaded=%s shadow_threshold=%.3f canary_enabled=%s canary_mode=%s canary_stage=%s canary_kill_switch=%s",
            self.policy["MODE_ID"],
            self.policy["MODE_LABEL"],
            self.policy["EVIDENCE_LABEL"],
            self.evidence_context.evidence_stream,
            self.evidence_context.account_scope,
            self.use_mock,
            self.strategy_id,
            self.policy.get("EXPERIMENT_TAG", "none"),
            self.policy.get("FIXED_RISK_USD"),
            non_srs_policy_allowed(),
            float(self.policy.get("MAX_SPREAD_PIPS") or 0.0),
            float(self.policy.get("ML_PREDICT_THRESHOLD") or 0.0),
            os.getenv("USE_SENTIMENT", "0"),
            self.ranker_loaded,
            self.shadow_runtime.enabled,
            self.shadow_runtime.model_loaded,
            self.shadow_runtime.config.threshold,
            self.canary_config.enabled,
            self.canary_config.mode,
            self.canary_config.enforce_stage,
            self.canary_config.kill_switch,
        )

        self.last_m15_candle: datetime | None = None
        self._last_m15_candle_by_symbol: dict[str, datetime] = self._load_last_m15_candles()
        if self._last_m15_candle_by_symbol:
            self.last_m15_candle = max(self._last_m15_candle_by_symbol.values())
        self._ensure_consecutive_losses_seeded()

    def _insert_trade_proposal(self, technical, *, status: str, reason_code: str, risk_percent: float, market_regime: str, **kwargs) -> None:
        try:
            self.repository.insert_trade_proposal(
                technical,
                status=status,
                reason_code=reason_code,
                risk_percent=risk_percent,
                market_regime=market_regime,
                evidence_context=self.evidence_context,
                **kwargs,
            )
        except TypeError as exc:
            if "evidence_context" not in str(exc):
                raise
            self.repository.insert_trade_proposal(
                technical,
                status=status,
                reason_code=reason_code,
                risk_percent=risk_percent,
                market_regime=market_regime,
                **kwargs,
            )

    def _insert_risk_event(
        self, rule_name: str, severity: str, reason: str, trade_id: str | None = None, *, alert: bool = True
    ) -> None:
        if alert and severity in ("BLOCK", "CRITICAL") and self.outage_monitor is not None:
            # A stale bridge feed is reported by the outage alert (two messages per
            # outage); the event is still persisted, only its own alert is dropped.
            try:
                if self.outage_monitor.reports(rule_name, datetime.now(timezone.utc)):
                    alert = False
                    logger.info("Risk alert %s folded into the bridge outage alert", rule_name)
            except Exception:
                logger.exception("Outage coverage check failed; sending the risk alert")
        _insert_risk_event_with_context(
            rule_name,
            severity,
            reason,
            trade_id,
            evidence_context=self.evidence_context,
            repository=self.repository,
            alert=alert,
        )

    def _insert_funnel_event(
        self,
        *,
        decision_time: datetime,
        stage: str,
        outcome: str,
        reason_code: str,
        symbol: str | None = None,
        details: str = "",
        trade_id: str | None = None,
    ) -> None:
        try:
            _insert_funnel_event_with_context(
                decision_time=decision_time,
                stage=stage,
                outcome=outcome,
                reason_code=reason_code,
                symbol=symbol,
                details=details,
                trade_id=trade_id,
                evidence_context=self.evidence_context,
                repository=self.repository,
            )
        except Exception as exc:
            logger.warning("Failed to persist funnel event stage=%s symbol=%s error=%s", stage, symbol, exc)

    def _insert_ml_shadow_event(
        self,
        *,
        decision_time: datetime,
        symbol: str,
        trade_id: str | None,
        stage: str,
        primary_gate_outcome: str,
        shadow_decision,
        veto: bool | None = None,
        risk_multiplier: float | None = None,
        would_be_risk_percent: float | None = None,
        latency_ms: float | None = None,
        strategy_id: str | None = None,
    ) -> None:
        try:
            self.repository.insert_ml_shadow_event(
                decision_time=decision_time,
                symbol=symbol,
                trade_id=trade_id,
                stage=stage,
                primary_gate_outcome=primary_gate_outcome,
                shadow_outcome=shadow_decision.outcome,
                reason_code=shadow_decision.reason_code,
                details=shadow_decision.details,
                probability=shadow_decision.probability,
                threshold=shadow_decision.threshold,
                model_loaded=shadow_decision.model_loaded,
                checkpoint_path=shadow_decision.checkpoint_path,
                feature_schema_version=shadow_decision.feature_schema_version,
                evidence_context=self.evidence_context,
                veto=veto,
                risk_multiplier=risk_multiplier,
                would_be_risk_percent=would_be_risk_percent,
                latency_ms=latency_ms,
                strategy_id=strategy_id,
            )
        except TypeError as exc:
            if "evidence_context" not in str(exc):
                raise
            self.repository.insert_ml_shadow_event(
                decision_time=decision_time,
                symbol=symbol,
                trade_id=trade_id,
                stage=stage,
                primary_gate_outcome=primary_gate_outcome,
                shadow_outcome=shadow_decision.outcome,
                reason_code=shadow_decision.reason_code,
                details=shadow_decision.details,
                probability=shadow_decision.probability,
                threshold=shadow_decision.threshold,
                model_loaded=shadow_decision.model_loaded,
                checkpoint_path=shadow_decision.checkpoint_path,
                feature_schema_version=shadow_decision.feature_schema_version,
            )
        except Exception as exc:
            logger.warning("Failed to persist ml shadow event symbol=%s error=%s", symbol, exc)

    def _virtual_balance(self) -> float:
        """Return the effective balance for lot-sizing, applying VIRTUAL_BALANCE_DIVISOR
        when running Preserve-10 on a standard (non-cent) account."""
        balance = self.account_status.balance
        divisor = float(self.policy.get("VIRTUAL_BALANCE_DIVISOR") or 1)
        if divisor > 1:
            balance = balance / divisor
        return balance

    def _pre_route_feasibility(self, symbol: str, risk_percent: float, stop_pips: float):
        fixed_risk_usd = self.policy.get("FIXED_RISK_USD")
        if fixed_risk_usd is None or float(fixed_risk_usd) <= 0:
            return None
        return self.bridge.evaluate_trade_feasibility(
            symbol,
            risk_percent,
            stop_pips,
            account_balance=self._virtual_balance(),
        )

    def _strategic_risk_eligibility(self, symbol: str, stop_pips: float):
        fixed_risk_usd = self.policy.get("FIXED_RISK_USD")
        if fixed_risk_usd is None or float(fixed_risk_usd) <= 0:
            return None
        return self.bridge.evaluate_fixed_risk_eligibility(
            symbol,
            float(fixed_risk_usd),
            stop_pips,
            account_balance=self._virtual_balance(),
        )

    def _fail_closed_bridge_uncertainty(self, reason: str) -> None:
        self.account_status.is_trading_halted = True
        self.account_status.state_reconciled = False
        if not self.account_status.state_reconciliation_reason:
            self.account_status.state_reconciliation_reason = reason
        elif reason not in self.account_status.state_reconciliation_reason:
            self.account_status.state_reconciliation_reason = (
                f"{self.account_status.state_reconciliation_reason}; {reason}"
            )

    def _fail_closed_preserve_10_bridge(self, reason: str) -> None:
        self._fail_closed_bridge_uncertainty(reason)

    def _enforce_execution_feedback_integrity(self) -> None:
        feedback_service.enforce_execution_feedback_integrity(self)

    def _validate_bridge_path(self, bridge_path: Path) -> None:
        required = ["pending_signals", "feedback", "exits", "active_locks"]
        missing = [name for name in required if not (bridge_path / name).exists()]
        if not missing:
            return
        if self.use_mock:
            logger.warning("Bridge path missing subfolders in mock mode. They will be created: %s", missing)
            return
        raise RuntimeError(
            f"Bridge path missing required folders in live mode: {missing}. "
            f"Set BRIDGE_BASE_PATH to your MT5 MQL5/Files/bridge directory."
        )

    def _update_account_state(self) -> None:
        market_data_service.update_account_state(self)

    def _ensure_consecutive_losses_seeded(self) -> None:
        if self._consecutive_losses_seeded:
            return
        persisted_state = self.repository.get_latest_account_metric(
            evidence_stream=self.evidence_context.evidence_stream,
            account_scope=self.evidence_context.account_scope,
        )
        if persisted_state is not None:
            self.account_status.consecutive_losses = int(
                persisted_state.get("consecutive_losses", self.account_status.consecutive_losses) or 0
            )
        self._consecutive_losses_seeded = True

    def _record_consecutive_loss_update(self, pnl: float) -> None:
        self._ensure_consecutive_losses_seeded()
        if pnl < 0:
            self.account_status.consecutive_losses += 1
            self._consecutive_losses_dirty = True
        elif pnl > 0:
            self.account_status.consecutive_losses = 0
            self._consecutive_losses_dirty = True
        else:
            return

        if self._loss_streak_unknown:
            # HT-009A-1E: the base count could not be read (reconciliation-only pass).
            if pnl > 0:
                # A win sets the streak to 0 whatever the base was: the count is known again.
                self._loss_streak_unknown = False
            else:
                # A loss on an unknown base fails closed at the halt threshold and is
                # persisted, so the halt holds until validation.reset_loss_streak.
                halt_threshold = int(self.policy.get("LOSS_HALT_THRESHOLD") or 3)
                self.account_status.consecutive_losses = halt_threshold
                try:
                    self._insert_risk_event(
                        "LOSS_STREAK_UNKNOWN",
                        "BLOCK",
                        f"loss applied while the pending loss-streak state was unreadable; "
                        f"consecutive_losses persisted at the halt threshold {halt_threshold}",
                        alert=False,
                    )
                except Exception:
                    logger.exception("Could not record LOSS_STREAK_UNKNOWN")

        if self.reconcile_only or not self._account_values_loaded:
            self._persist_loss_streak_checkpoint()
            return

        try:
            self.account_status.updated_at = datetime.now(timezone.utc)
            self.repository.insert_account_metrics(
                self.account_status,
                evidence_context=self.evidence_context,
            )
            self._consecutive_losses_dirty = False
            self._clear_pending_loss_streak()
        except Exception:
            logger.exception("Failed to persist consecutive loss throttle checkpoint; retrying on next cycle")
            self._consecutive_losses_dirty = True

    def _persist_loss_streak_checkpoint(self) -> None:
        """HT-009A-1: persist a loss-streak change made before account values were read.

        An exit applied before the first account update (a restart with an exit
        waiting, or the reconciliation-only pass of a refused start) would otherwise
        persist the constructor defaults (balance and equity 0) and, in a
        reconciliation-only engine, the forced halt. The checkpoint copies the latest
        persisted account metric and changes only the consecutive-loss count and the
        timestamp. With no metric to copy, the count goes into scoped engine runtime
        state (``pending_loss_streak``), a BLOCK event records the deferral, and the
        change stays dirty. The next engine loads that count before any decision; it
        is cleared only once a metric with real account values (or a copy) holds it.
        """
        losses = self.account_status.consecutive_losses
        try:
            latest = self.repository.get_latest_account_metric(
                evidence_stream=self.evidence_context.evidence_stream,
                account_scope=self.evidence_context.account_scope,
            )
            if latest is None:
                self._consecutive_losses_dirty = True
                if self._pending_loss_streak_active and self._pending_loss_streak_value == losses:
                    return  # already pending (the loop retries every iteration while stale)
                self._set_pending_loss_streak(losses)
                self._insert_risk_event(
                    "LOSS_STREAK_CHECKPOINT_DEFERRED",
                    "BLOCK",
                    (
                        f"consecutive_losses={losses} kept as pending runtime state: no account metric to copy "
                        "before the first account update"
                    ),
                    alert=False,
                )
                return
            checkpoint = AccountStatus(
                balance=float(latest.get("balance") or 0.0),
                equity=float(latest.get("equity") or 0.0),
                open_risk_percent=float(latest.get("open_risk_percent") or 0.0),
                open_usd_exposure_count=int(latest.get("open_usd_exposure_count") or 0),
                daily_loss_percent=float(latest.get("daily_loss_percent") or 0.0),
                weekly_loss_percent=float(latest.get("weekly_loss_percent") or 0.0),
                drawdown_percent=float(latest.get("drawdown_percent") or 0.0),
                peak_equity=float(latest.get("peak_equity") or 0.0),
                daily_anchor_date=str(latest.get("daily_anchor_date") or ""),
                daily_anchor_equity=float(latest.get("daily_anchor_equity") or 0.0),
                weekly_anchor_key=str(latest.get("weekly_anchor_key") or ""),
                weekly_anchor_equity=float(latest.get("weekly_anchor_equity") or 0.0),
                consecutive_losses=losses,
                is_trading_halted=bool(latest.get("is_trading_halted")),
                updated_at=datetime.now(timezone.utc),
            )
            self.repository.insert_account_metrics(checkpoint, evidence_context=self.evidence_context)
            self._consecutive_losses_dirty = False
            self._clear_pending_loss_streak()
        except Exception:
            logger.exception("Failed to persist the loss-streak checkpoint; retrying with the next account update")
            self._consecutive_losses_dirty = True

    def _load_pending_loss_streak(self) -> None:
        """HT-009A-1: a loss-streak count an earlier process could not put in a metric.

        The count is pushed into the first account update (dirty flag) and so reaches
        the Hard Risk Engine before any decision cycle. A metric written after it holds
        the newer count and wins (the stale pending state is cleared); when the order
        is unknown the higher count wins. A malformed value (not a non-negative whole
        number) fails closed at the halt threshold; an unreadable state refuses the start.
        """
        halt_threshold = int(self.policy.get("LOSS_HALT_THRESHOLD") or 3)
        try:
            raw = self.repository.get_engine_runtime_state(
                PENDING_LOSS_STREAK_STATE_KEY, evidence_context=self.evidence_context
            )
        except Exception as exc:
            if self.reconcile_only:
                # HT-009A-1E: the reconciliation-only pass must still apply exits. It never
                # routes. The count is held at the threshold in memory; nothing is persisted
                # unless a close changes it (see _loss_streak_unknown in
                # _record_consecutive_loss_update), so a pass without a loss latches nothing.
                logger.exception(
                    "Pending loss-streak state unreadable in the reconciliation-only pass; "
                    "holding consecutive_losses at %s in memory",
                    halt_threshold,
                )
                self.account_status.consecutive_losses = halt_threshold
                self._consecutive_losses_seeded = True
                self._loss_streak_unknown = True
                return
            # Fail closed without latching a halt: a pending count may exist and cannot be
            # read, so the engine does not start (main returns 2 on this RuntimeError).
            raise RuntimeError(f"Pending loss-streak state unreadable: {exc}") from exc
        if not raw:
            return
        pending, recorded_at = _parse_pending_loss_streak(raw)
        if pending is None:
            # HT-009A-1D: anything but a non-negative whole number fails closed.
            logger.error("Malformed pending loss-streak state %r; failing closed at %s", raw, halt_threshold)
            self._apply_pending_loss_streak(halt_threshold)
            return

        # HT-009A-1C: a metric written after the pending count (the clear failed or the
        # process stopped between the two writes) holds the newer, authoritative count.
        try:
            latest = self.repository.get_latest_account_metric(
                evidence_stream=self.evidence_context.evidence_stream,
                account_scope=self.evidence_context.account_scope,
            )
        except Exception:
            logger.exception("Latest account metric unreadable; using the pending loss streak")
            latest = None
        if latest is not None:
            metric_losses = _strict_non_negative_int(latest.get("consecutive_losses"))
            metric_at = _parse_utc_timestamp(latest.get("timestamp"))
            if metric_losses is None:
                logger.error("Latest metric has a malformed loss streak; failing closed at %s", halt_threshold)
                self._apply_pending_loss_streak(max(pending, halt_threshold))
                return
            if metric_at is not None and recorded_at is not None and metric_at > recorded_at:
                logger.warning(
                    "Stale pending loss streak %s ignored: newer metric (%s) holds consecutive_losses=%s",
                    pending, metric_at.isoformat(), metric_losses,
                )
                self.account_status.consecutive_losses = metric_losses
                self._consecutive_losses_seeded = True
                self._pending_loss_streak_active = True
                self._clear_pending_loss_streak()
                return
            if metric_at is None or recorded_at is None or metric_at == recorded_at:
                pending = max(pending, metric_losses)  # order unknown: the higher count
        self._apply_pending_loss_streak(pending)

    def _apply_pending_loss_streak(self, losses: int) -> None:
        self.account_status.consecutive_losses = losses
        self._consecutive_losses_seeded = True
        self._consecutive_losses_dirty = True
        self._pending_loss_streak_active = True
        self._pending_loss_streak_value = losses
        logger.warning("Pending loss streak restored: consecutive_losses=%s", losses)

    def _set_pending_loss_streak(self, losses: int) -> None:
        self.repository.set_engine_runtime_state(
            PENDING_LOSS_STREAK_STATE_KEY,
            json.dumps({"consecutive_losses": int(losses), "recorded_at": datetime.now(timezone.utc).isoformat()}),
            evidence_context=self.evidence_context,
        )
        self._pending_loss_streak_active = True
        self._pending_loss_streak_value = int(losses)

    def _clear_pending_loss_streak(self) -> None:
        """Called only after a metric with real account values (or a copied one) holds the count."""
        if not self._pending_loss_streak_active:
            return
        try:
            self.repository.set_engine_runtime_state(
                PENDING_LOSS_STREAK_STATE_KEY, "", evidence_context=self.evidence_context
            )
            self._pending_loss_streak_active = False
            self._pending_loss_streak_value = None
        except Exception:
            logger.exception("Could not clear the pending loss-streak state; it is cleared after the next metric")

    def _after_account_metrics_persisted(self) -> None:
        """End-of-loop metric write: clears the pending count once real account values hold it."""
        if self._account_values_loaded:
            self._clear_pending_loss_streak()

    def _record_broker_close_time(self, close_time_value: object) -> None:
        feedback_service.record_broker_close_time(self, close_time_value)

    def _is_snapshot_stale_after_broker_close(self, snapshot: Mapping[str, object]) -> bool:
        return feedback_service.is_snapshot_stale_after_broker_close(self, snapshot)

    def _fetch_broker_closure_summary(
        self,
        *,
        local_position_ticket: int,
        trade_id: str,
    ) -> dict[str, object] | None:
        return feedback_service.fetch_broker_closure_summary(
            self, local_position_ticket=local_position_ticket, trade_id=trade_id
        )

    def _recover_missing_trade_closures_from_broker(self) -> None:
        feedback_service.recover_missing_trade_closures_from_broker(self)

    def _consume_feedback(self) -> None:
        feedback_service.consume_feedback(self)

    def _is_new_m15_candle(self) -> bool:
        return market_data_service.is_new_m15_candle(self)

    def _load_last_m15_candles(self) -> dict[str, datetime]:
        return market_data_service.load_last_m15_candles(self)

    def _store_last_m15_candles(self) -> None:
        market_data_service.store_last_m15_candles(self)

    def _evaluate_symbol(self, sym: str, decision_time: datetime, session_name: str | None) -> None:
        decision_cycle.evaluate_symbol(self, sym, decision_time, session_name)

    def _decision_cycle(self) -> None:
        if self.reconcile_only:
            logger.error("Decision cycle refused: reconciliation-only engine (start refused by a policy gate)")
            return
        decision_cycle.run_decision_cycle(self)

    def _poll_host_guard(
        self, now: datetime | None = None, *, force: bool = False
    ) -> host_tolerance.HostGuardStatus | None:
        if self.host_guard is None:
            return None
        # Persisted without a Telegram risk alert: a stale bridge is reported by
        # the outage monitor (two messages per outage), not by this event.
        record = host_tolerance.persisting_recorder(
            self.host_guard,
            lambda rule, severity, reason: self._insert_risk_event(rule, severity, reason, alert=False),
            self.repository,
            self.evidence_context,
        )
        return self.host_guard.poll(now or datetime.now(timezone.utc), record, force=force)

    def _poll_outage_monitor(self, now: datetime | None = None) -> None:
        if self.outage_monitor is None:
            return
        try:
            self.outage_monitor.poll(now or datetime.now(timezone.utc))
        except Exception:
            logger.exception("Outage monitor failed; continuing")

    def run(self, mode: str, iterations: int = 0) -> None:
        if self.reconcile_only:
            raise RuntimeError("A reconciliation-only engine cannot run the decision loop")
        engine_runtime.run(self, mode, iterations)

    def reconcile_once(self) -> None:
        engine_runtime.reconcile_once(self)


def _resolve_runtime_policy(args: argparse.Namespace) -> dict:
    """The run's policy before the demo-only experiment gate (main and refused starts)."""
    if args.policy_mode:
        os.environ["FX_POLICY_MODE"] = args.policy_mode
    policy = apply_runtime_experiment_config(get_policy_config(), run_mode=args.mode, env=os.environ)
    if args.policy_mode and policy["MODE_ID"] != args.policy_mode:
        policy = apply_runtime_experiment_config(get_policy_config(mode_id=args.policy_mode), run_mode=args.mode, env=os.environ)
    return policy


def _record_host_guard_at_refused_start(
    args: argparse.Namespace, bridge: MT5Connection, *, repository: EngineRepository, use_mock: bool
) -> None:
    """A demo start refused for want of a fresh demo snapshot still runs the host guard.

    The BLOCK (for example a stale snapshot) is persisted with the evidence context
    the engine would have used, together with the guard state, so the engine that
    starts once the host passes records HOST_GUARD_RECOVERED. The start stays refused.
    """
    try:
        context = build_runtime_evidence_context(
            _resolve_runtime_policy(args),
            use_mock=use_mock,
            login=getattr(bridge, "login", 0),
            server=getattr(bridge, "server", ""),
        )
        guard = host_tolerance.build_host_guard(use_mock=use_mock, run_mode=args.mode, bridge_path=get_mt5_bridge_path())
        host_tolerance.restore_guard_state(guard, repository, context)
        record = host_tolerance.persisting_recorder(
            guard,
            lambda rule, severity, reason: _insert_risk_event_with_context(
                rule, severity, reason, evidence_context=context, repository=repository, alert=False
            ),
            repository,
            context,
        )
        status = guard.poll(datetime.now(timezone.utc), record, force=True)
        if status.blocked:
            logger.error("Host guard BLOCK at the refused start: %s", status.details)
    except Exception:
        logger.exception("Host guard could not be evaluated at the refused start")


def _reconcile_before_refused_start(
    args: argparse.Namespace,
    bridge: MT5Connection,
    tracer,
    metrics,
    *,
    policy: Mapping[str, object],
    repository: EngineRepository,
    use_mock: bool,
) -> None:
    """HT-009A: a demo start refused by a policy gate still reconciles exits once.

    A trade opened under a non-SRS or Preserve-$10 policy can close at the broker
    (TP or SL) while the host is down. If the next start is refused, for example
    because the account is unverified on a stale snapshot, the close must still be
    applied and its reservation released. The reconciliation-only engine never runs
    a decision cycle or routes a signal; the start stays refused.
    """
    if args.mode != "demo":
        return
    try:
        engine = Engine(
            bridge,
            tracer,
            metrics,
            use_mock=use_mock,
            run_mode=args.mode,
            policy=policy,
            repository=repository,
            reconcile_only=True,
        )
        engine.reconcile_once()
        logger.info("Reconciliation-only pass completed for the refused start")
    except Exception:
        logger.exception("Reconciliation-only pass failed for the refused start")


def _send_start_notice(mode: str, *, use_mock: bool) -> None:
    """The "engine started" message, unless it belongs to an outage already reported."""
    from core.alerts import send_telegram_message

    try:
        monitor = None if use_mock else host_tolerance.build_outage_monitor(
            use_mock=False, bridge_path=get_mt5_bridge_path()
        )
        if monitor is not None and not monitor.claim_start_notice():
            logger.info("Engine start notice folded into the bridge outage alert")
            return
    except Exception:
        logger.exception("Outage state unavailable; sending the start notice")
    send_telegram_message("🚀 <b>FX AI Engine Started</b>\nMode: " + mode)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FX AI Engine")
    parser.add_argument("--mode", choices=["smoke", "demo", "live"], default="smoke")
    parser.add_argument(
        "--policy-mode",
        choices=sorted(MODE_CONFIGS),
        default=None,
        help=(
            "Explicit runtime policy mode. Non-SRS modes are research-only and require "
            f"{ALLOW_NON_SRS_POLICY_ENV}=1 plus USE_MT5_MOCK=1 or a verified demo account."
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Optional loop iterations for demo mode. 0 means run until stopped.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)
    load_runtime_env()

    run_all_migrations()
    repository = SqliteEngineRepository()

    use_mock = os.getenv("USE_MT5_MOCK") == "1"
    # MT5_CONNECTION=file_bridge reads EA exports from the bridge folder instead
    # of the Windows-only MetaTrader5 package (ADR 0001, D4). Mock mode wins.
    use_file_bridge = (not use_mock) and os.getenv("MT5_CONNECTION", "").strip().lower() == "file_bridge"
    if use_mock or use_file_bridge:
        creds = None
    else:
        try:
            creds = load_mt5_credentials_from_env()
        except CredentialsError as exc:
            logger.error("Credential error: %s", exc)
            return 1

    tracer = init_tracing(os.getenv("OTEL_SERVICE_NAME", "fx_ai_engine"))
    metrics = init_metrics()

    if use_file_bridge:
        try:
            bridge = FileBridgeConnection(get_mt5_bridge_path())
        except RuntimeError as exc:
            logger.error("File bridge path unavailable: %s", exc)
            return 2
    else:
        bridge = MT5Connection(
            creds.login if creds else 0,
            creds.password if creds else "",
            creds.server if creds else "",
        )
    if not bridge.connect():
        logger.error("MT5 connection failed: %s", bridge.last_error)
        return 2

    account_state: bool | None = True
    if args.mode == "demo":
        # HT-009: only a confirmed live account refuses the start. An unverified
        # demo account (stale or unreadable snapshot, for example after downtime)
        # starts into the runtime loop, which blocks entries but still applies
        # feedback, exits and broker closure recovery, and records the host guard.
        account_state = _demo_account_state(bridge, use_mock=use_mock)
        if account_state is False:
            logger.error("Demo mode requires a demo account; the terminal reports a live account; refusing to start")
            _record_host_guard_at_refused_start(args, bridge, repository=repository, use_mock=use_mock)
            return 2
        if account_state is None:
            logger.warning(
                "Demo account unverified at start (stale or unreadable snapshot); "
                "starting with new entries blocked so feedback and exits still reconcile"
            )

    live_session = None
    if args.mode == "live":
        live_session = live_gate.startup_gate(
            bridge,
            policy_mode_arg=args.policy_mode,
            use_mock=use_mock,
            running_strategy_id=globals().get("LIVE_STRATEGY_ID"),
        )
        if live_session is None:
            return 2
        os.environ["FX_POLICY_MODE"] = live_gate.LIVE_POLICY_MODE

    start_health_api(int(os.getenv("HEALTH_PORT", "8080")))
    if args.mode == "demo" and account_state is None:
        # The bridge is stale: this start belongs to that episode, which the outage
        # monitor reports (one outage and one recovery message), not a start notice.
        logger.info("Engine start notice skipped: demo account unverified (bridge stale)")
    else:
        _send_start_notice(args.mode, use_mock=use_mock)

    try:
        policy = _enforce_demo_only_experiment_account_gate(
            _resolve_runtime_policy(args), bridge=bridge, use_mock=use_mock
        )
        startup_evidence_context = build_runtime_evidence_context(
            policy,
            use_mock=use_mock,
            login=getattr(bridge, "login", 0),
            server=getattr(bridge, "server", ""),
        )
        policy_approval = evaluate_runtime_policy_approval(
            bridge,
            policy=policy,
            use_mock=use_mock,
            env=os.environ,
        )
        if is_non_srs_policy_mode(policy["MODE_ID"]):
            severity = "INFO" if policy_approval.approved else "BLOCK"
            _insert_risk_event_with_context(
                "NON_SRS_POLICY_APPROVAL",
                severity,
                f"{policy_approval.reason_code} {policy_approval.details}",
                evidence_context=startup_evidence_context,
                repository=repository,
            )
            if policy_approval.approved:
                logger.warning(
                    "Non-SRS runtime policy approved: reason_code=%s details=%s",
                    policy_approval.reason_code,
                    policy_approval.details,
                )
            else:
                logger.error(
                    "Non-SRS runtime policy blocked: reason_code=%s details=%s",
                    policy_approval.reason_code,
                    policy_approval.details,
                )
                _reconcile_before_refused_start(
                    args, bridge, tracer, metrics, policy=policy, repository=repository, use_mock=use_mock
                )
                return 4
        startup_approval = evaluate_preserve_10_startup_approval(
            bridge,
            policy=policy,
            env=os.environ,
        )
        if policy["MODE_ID"] == "preserve_10":
            severity = "INFO" if startup_approval.approved else "BLOCK"
            _insert_risk_event_with_context(
                "PRESERVE_10_STARTUP_APPROVAL",
                severity,
                f"{startup_approval.reason_code} {startup_approval.details}",
                evidence_context=startup_evidence_context,
                repository=repository,
            )
            if startup_approval.approved:
                logger.info(
                    "Preserve-$10 startup approval passed: reason_code=%s details=%s",
                    startup_approval.reason_code,
                    startup_approval.details,
                )
            else:
                logger.error(
                    "Preserve-$10 startup approval refused: reason_code=%s details=%s",
                    startup_approval.reason_code,
                    startup_approval.details,
                )
                _reconcile_before_refused_start(
                    args, bridge, tracer, metrics, policy=policy, repository=repository, use_mock=use_mock
                )
                return 3

        try:
            engine = Engine(
                bridge, tracer, metrics, use_mock=use_mock, run_mode=args.mode, policy=policy, repository=repository
            )
        except RuntimeError as exc:
            logger.error("Engine initialization failed: %s", exc)
            return 2
        if live_session is not None and not live_session.attach(engine):
            return 2
        engine.run(mode=args.mode, iterations=args.iterations)
    finally:
        bridge.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
