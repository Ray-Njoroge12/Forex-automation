from __future__ import annotations

import argparse
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
from application.routing_service import RoutingService
from core.account_status import AccountStatus
from core.bridge_utils import get_mt5_bridge_path
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.credentials import CredentialsError, load_mt5_credentials_from_env
from core.evidence import build_runtime_evidence_context
from core.env_loader import load_runtime_env
from core.sentiment.sentiment_agent import SentimentAgent
from ml.signal_ranker import SignalRanker
from ml.meta_labeler.shadow_runtime import (
    MetaLabelerShadowRuntime,
    resolve_canary_runtime_config,
)
from core.logging_utils import configure_logging

load_runtime_env()
from core.alerts import alert_risk_halt
from core.health import start_health_api
from core.metrics import init_metrics
from core.mt5_bridge import MT5Connection
from core.file_bridge_connection import FileBridgeConnection
from core.observability import init_tracing
from core.risk.hard_risk_engine import HardRiskEngine
from core.risk.reset_scheduler import ResetScheduler
from database.db import (
    get_latest_account_metric,
    initialize_schema,
    insert_decision_funnel_event,
    insert_risk_event,
    insert_account_metrics,
    insert_trade_proposal,
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
    insert_ml_shadow_event,
)
# Engine responsibilities live in application/* (Phase 4 split). main.py is the
# composition root: start-up approvals, Engine wiring and one-line delegates.
# Names that moved are re-exported here so existing imports keep working.
from application import decision_cycle, engine_runtime, feedback_service, market_data_service
from application.decision_cycle import (  # noqa: F401
    _format_pre_route_feasibility_event,
    _format_strategic_risk_event,
    _normalize_pre_route_feasibility_details,
    _policy_evidence_suffix,
    _with_policy_evidence,
)
from application.engine_runtime import _demo_account_verified  # noqa: F401
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
) -> None:
    if severity in ("BLOCK", "CRITICAL"):
        alert_risk_halt(rule_name, reason, severity)
    try:
        insert_risk_event(
            rule_name,
            severity,
            reason,
            trade_id,
            evidence_context=evidence_context,
        )
    except TypeError as exc:
        if "evidence_context" not in str(exc):
            raise
        insert_risk_event(rule_name, severity, reason, trade_id)


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
) -> None:
    try:
        insert_decision_funnel_event(
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
        insert_decision_funnel_event(
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
    def __init__(
        self,
        bridge: MT5Connection,
        tracer,
        metrics,
        use_mock: bool,
        *,
        run_mode: str,
        policy: Mapping[str, object] | None = None,
    ):
        self.bridge = bridge
        self.tracer = tracer
        self.metrics = metrics
        self.use_mock = use_mock
        self.run_mode = run_mode
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
        if is_non_srs_policy_mode(self.policy["MODE_ID"]) and not runtime_policy_approval.approved:
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
        if self.policy["MODE_ID"] == "preserve_10" and not preserve_startup_approval.approved:
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

        # Auto-detect MT5 sandbox bridge path
        self.bridge_path = get_mt5_bridge_path()
        self._validate_bridge_path(self.bridge_path)
        logger.info("Using MT5 bridge path: %s", self.bridge_path)

        self.router = SignalRouter(
            pending_dir=self.bridge_path / "pending_signals",
            lock_dir=self.bridge_path / "active_locks",
            require_database_dedup=True,
        )
        self.routing_service = RoutingService(self.router, self.policy)
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

        self.agents = {
            sym: {
                "regime": RegimeAgent(sym, bridge.fetch_ohlc_data, policy=self.policy),
                "technical": TechnicalAgent(
                    sym,
                    bridge.fetch_ohlc_data,
                    bridge.get_live_spread,
                    policy=self.policy,
                ),
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
            "Runtime config: mode=%s label=%s evidence=%s stream=%s account_scope=%s mock=%s experiment=%s fixed_risk_usd=%s allow_non_srs_policy=%s max_spread_pips=%s ml_threshold=%.3f sentiment=%s ranker_loaded=%s shadow_enabled=%s shadow_model_loaded=%s shadow_threshold=%.3f canary_enabled=%s canary_mode=%s canary_stage=%s canary_kill_switch=%s",
            self.policy["MODE_ID"],
            self.policy["MODE_LABEL"],
            self.policy["EVIDENCE_LABEL"],
            self.evidence_context.evidence_stream,
            self.evidence_context.account_scope,
            self.use_mock,
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
            insert_trade_proposal(
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
            insert_trade_proposal(
                technical,
                status=status,
                reason_code=reason_code,
                risk_percent=risk_percent,
                market_regime=market_regime,
                **kwargs,
            )

    def _insert_risk_event(self, rule_name: str, severity: str, reason: str, trade_id: str | None = None) -> None:
        _insert_risk_event_with_context(
            rule_name,
            severity,
            reason,
            trade_id,
            evidence_context=self.evidence_context,
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
    ) -> None:
        try:
            insert_ml_shadow_event(
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
            )
        except TypeError as exc:
            if "evidence_context" not in str(exc):
                raise
            insert_ml_shadow_event(
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
        persisted_state = get_latest_account_metric(
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

        try:
            self.account_status.updated_at = datetime.now(timezone.utc)
            insert_account_metrics(
                self.account_status,
                evidence_context=self.evidence_context,
            )
            self._consecutive_losses_dirty = False
        except Exception:
            logger.exception("Failed to persist consecutive loss throttle checkpoint; retrying on next cycle")
            self._consecutive_losses_dirty = True

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
        decision_cycle.run_decision_cycle(self)

    def run(self, mode: str, iterations: int = 0) -> None:
        engine_runtime.run(self, mode, iterations)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FX AI Engine")
    parser.add_argument("--mode", choices=["smoke", "demo"], default="smoke")
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

    if args.mode == "demo" and not _demo_account_verified(bridge, use_mock=use_mock):
        logger.error("Demo mode requires a verified MT5 demo account; refusing to start")
        return 2

    start_health_api(int(os.getenv("HEALTH_PORT", "8080")))
    from core.alerts import send_telegram_message
    send_telegram_message("🚀 <b>FX AI Engine Started</b>\nMode: " + args.mode)

    try:
        if args.policy_mode:
            os.environ["FX_POLICY_MODE"] = args.policy_mode
        policy = apply_runtime_experiment_config(get_policy_config(), run_mode=args.mode, env=os.environ)
        if args.policy_mode and policy["MODE_ID"] != args.policy_mode:
            policy = apply_runtime_experiment_config(get_policy_config(mode_id=args.policy_mode), run_mode=args.mode, env=os.environ)
        policy = _enforce_demo_only_experiment_account_gate(policy, bridge=bridge, use_mock=use_mock)
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
                return 3

        try:
            engine = Engine(bridge, tracer, metrics, use_mock=use_mock, run_mode=args.mode, policy=policy)
        except RuntimeError as exc:
            logger.error("Engine initialization failed: %s", exc)
            return 2
        engine.run(mode=args.mode, iterations=args.iterations)
    finally:
        bridge.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
