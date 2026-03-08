from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from config_microcapital import (
    get_policy_config,
    read_fixed_risk_usd,
    read_max_spread_pips,
    read_predict_threshold,
)
from bridge.execution_feedback import ExecutionFeedbackReader
from bridge.mock_feedback_simulator import MockFeedbackSimulator
from bridge.signal_router import SignalRouteError, SignalRouter
from core.account_status import AccountStatus
from core.bridge_utils import get_mt5_bridge_path
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.credentials import CredentialsError, load_mt5_credentials_from_env
from core.evidence import build_runtime_evidence_context
from core.env_loader import load_runtime_env
from core.filters.calendar_filter import CalendarEvent, is_news_blackout, load_calendar
from core.filters.macro_filter import load_rate_differentials
from core.filters.session_filter import get_active_session, is_tradeable_session
from core.sentiment.sentiment_agent import SentimentAgent
from ml.signal_ranker import SignalRanker
from core.logging_utils import configure_logging

load_runtime_env()
from core.metrics import init_metrics
from core.mt5_bridge import MT5Connection
from core.observability import init_tracing
from core.risk.hard_risk_engine import HardRiskEngine
from core.risk.reset_scheduler import ResetScheduler
from core.schemas import technical_signal_to_payload
from core.state_sync import update_account_status_from_snapshot
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15
from database.db import (
    get_latest_account_metric,
    get_open_trade_ledger,
    initialize_schema,
    insert_decision_funnel_event,
    insert_risk_event,
    insert_account_metrics,
    insert_trade_proposal,
    mark_trade_execution_uncertain,
    mark_trade_expired,
    migrate_add_decision_funnel_events,
    migrate_add_evidence_partition_columns,
    migrate_add_ml_feature_columns,
    migrate_add_risk_events,
    migrate_add_restart_state_columns,
    migrate_phase8_columns,
    update_trade_execution_result,
    update_trade_exit_result,
)

logger = logging.getLogger("fx_ai_engine.main")

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"]
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


def _startup_approve(reason_code: str, details: str) -> Preserve10StartupApprovalDecision:
    return Preserve10StartupApprovalDecision(True, reason_code, details)


def _startup_reject(reason_code: str, details: str) -> Preserve10StartupApprovalDecision:
    return Preserve10StartupApprovalDecision(False, reason_code, details)


def _policy_evidence_suffix(policy: Mapping[str, object]) -> str:
    return f"[mode={policy['MODE_ID']} evidence={policy['EVIDENCE_LABEL']}]"


def _with_policy_evidence(policy: Mapping[str, object], details: str) -> str:
    return f"{details} {_policy_evidence_suffix(policy)}"


def _normalize_preserve_10_feasibility_details(feasibility) -> str:
    details = str(feasibility.details)
    if not feasibility.can_assess:
        return details.replace(
            "symbol execution contract unavailable",
            "contract data is unavailable",
        )
    return f"{details}; trade blocked before MT5 routing"


def _insert_risk_event_with_context(
    rule_name: str,
    severity: str,
    reason: str,
    trade_id: str | None = None,
    *,
    evidence_context=None,
) -> None:
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


def _format_preserve_10_preroute_event(
    policy: Mapping[str, object],
    feasibility,
) -> str:
    outcome = "blocked before MT5 routing" if not feasibility.can_assess else "refused before MT5 routing"
    return _with_policy_evidence(
        policy,
        f"Preserve-$10 pre-route feasibility {outcome}: {_normalize_preserve_10_feasibility_details(feasibility)}",
    )


def _pip_size(symbol: str) -> float:
    return 0.01 if symbol.endswith("JPY") else 0.0001


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
    def __init__(self, bridge: MT5Connection, tracer, metrics, use_mock: bool):
        self.bridge = bridge
        self.tracer = tracer
        self.metrics = metrics
        self.use_mock = use_mock
        self.account_status = AccountStatus()
        self.reset_scheduler = ResetScheduler()
        self.policy = get_policy_config()
        self.evidence_context = build_runtime_evidence_context(
            self.policy,
            use_mock=use_mock,
            login=getattr(bridge, "login", 0),
            server=getattr(bridge, "server", ""),
        )
        self.predict_threshold = _predict_threshold()
        self._stale_episode_active = False
        self._last_reconciliation_reason = ""
        self._processed_execution_feedback: set[tuple[str, int, str, str]] = set()
        self._processed_exit_feedback: set[tuple[str, int, str, str]] = set()

        # Auto-detect MT5 sandbox bridge path
        self.bridge_path = get_mt5_bridge_path()
        self._validate_bridge_path(self.bridge_path)
        logger.info("Using MT5 bridge path: %s", self.bridge_path)

        self.router = SignalRouter(
            pending_dir=self.bridge_path / "pending_signals",
            lock_dir=self.bridge_path / "active_locks"
        )
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
        _data_dir = os.path.join(os.path.dirname(__file__), "data")
        self._calendar_events: list[CalendarEvent] = load_calendar(
            os.path.join(_data_dir, "economic_calendar.json")
        )
        self._rate_diffs = load_rate_differentials(
            os.path.join(_data_dir, "rate_differentials.json")
        )

        # Shared sentiment agent (lazy model load, 15-min cache per symbol).
        # Only active when USE_SENTIMENT=1.
        _sentiment = SentimentAgent()

        self.agents = {
            sym: {
                "regime": RegimeAgent(sym, bridge.fetch_ohlc_data),
                "technical": TechnicalAgent(sym, bridge.fetch_ohlc_data, bridge.get_live_spread),
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
        self.portfolio_manager = PortfolioManager(fetch_ohlc=bridge.fetch_ohlc_data)
        self.hard_risk = HardRiskEngine()
        self.ranker = SignalRanker()
        self.ranker_loaded = self.ranker.load()
        logger.info(
            "Runtime config: mode=%s label=%s evidence=%s stream=%s account_scope=%s mock=%s fixed_risk_usd=%s legacy_micro_capital=%s max_spread_pips=%s ml_threshold=%.3f sentiment=%s ranker_loaded=%s",
            self.policy["MODE_ID"],
            self.policy["MODE_LABEL"],
            self.policy["EVIDENCE_LABEL"],
            self.evidence_context.evidence_stream,
            self.evidence_context.account_scope,
            self.use_mock,
            read_fixed_risk_usd(),
            os.getenv("MICRO_CAPITAL_MODE", "0"),
            read_max_spread_pips(),
            self.predict_threshold,
            os.getenv("USE_SENTIMENT", "0"),
            self.ranker_loaded,
        )

        self.last_m15_candle: datetime | None = None

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

    def _preserve_10_pre_route_feasibility(self, symbol: str, risk_percent: float, stop_pips: float):
        if self.policy["MODE_ID"] != "preserve_10":
            return None
        return self.bridge.evaluate_trade_feasibility(
            symbol,
            risk_percent,
            stop_pips,
            account_balance=self.account_status.balance,
        )

    def _fail_closed_preserve_10_bridge(self, reason: str) -> None:
        self.account_status.is_trading_halted = True
        self.account_status.state_reconciled = False
        if not self.account_status.state_reconciliation_reason:
            self.account_status.state_reconciliation_reason = reason
        elif reason not in self.account_status.state_reconciliation_reason:
            self.account_status.state_reconciliation_reason = (
                f"{self.account_status.state_reconciliation_reason}; {reason}"
            )

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
        with self.tracer.start_as_current_span("update_account_state") as span:
            snapshot = self.feedback.read_account_snapshot()
            if snapshot is not None and snapshot.get("snapshot_source") == "mock_feedback_simulator" and not self.use_mock:
                logger.warning("Ignoring mock-generated account snapshot while running in non-mock mode")
                snapshot = None
            if snapshot is None:
                snapshot = self.bridge.get_account_snapshot() or {}

            persisted_state = get_latest_account_metric(
                evidence_stream=self.evidence_context.evidence_stream,
                account_scope=self.evidence_context.account_scope,
            )
            trade_ledger = get_open_trade_ledger(
                evidence_stream=self.evidence_context.evidence_stream,
                account_scope=self.evidence_context.account_scope,
            )

            update_account_status_from_snapshot(
                self.account_status,
                snapshot,
                persisted_state=persisted_state,
                trade_ledger=trade_ledger,
            )

            if not self.account_status.state_reconciled:
                if self.account_status.state_reconciliation_reason != self._last_reconciliation_reason:
                    self._insert_risk_event(
                        "STATE_RECONCILIATION_FAILED",
                        "BLOCK",
                        self.account_status.state_reconciliation_reason,
                    )
                    logger.warning(
                        "Account state reconciliation failed: %s",
                        self.account_status.state_reconciliation_reason,
                    )
                self._last_reconciliation_reason = self.account_status.state_reconciliation_reason
            elif self._last_reconciliation_reason:
                self._insert_risk_event("STATE_RECONCILED", "INFO", "Account state reconciled")
                logger.info("Account state reconciled after previous mismatch.")
                self._last_reconciliation_reason = ""

            if self.reset_scheduler.should_reset_daily():
                self.account_status.daily_loss_percent = 0.0
            if self.reset_scheduler.should_reset_weekly():
                self.account_status.weekly_loss_percent = 0.0

            span.set_attribute("open_positions", self.account_status.open_positions_count)
            span.set_attribute("drawdown_percent", self.account_status.drawdown_percent)

        self.metrics.set_gauge("open_positions", self.account_status.open_positions_count)
        self.metrics.set_gauge("open_risk_percent", self.account_status.open_risk_percent)

    def _consume_feedback(self) -> None:
        with self.tracer.start_as_current_span("consume_feedback"):
            # Consume new entry executions
            for payload in self.feedback.consume_execution_feedback():
                key = (
                    str(payload.get("trade_id", "")),
                    int(payload.get("ticket", 0)),
                    str(payload.get("status", "")),
                    str(payload.get("close_time", "")),
                )
                if key in self._processed_execution_feedback:
                    continue
                self._processed_execution_feedback.add(key)
                update_trade_execution_result(payload)
                self.router.release_lock(str(payload.get("trade_id", "")))

            # Consume trade exits (stops and take profits)
            for payload in self.feedback.consume_trade_exits():
                key = (
                    str(payload.get("trade_id", "")),
                    int(payload.get("position_ticket") or payload.get("ticket", 0)),
                    str(payload.get("status", "")),
                    str(payload.get("close_time", "")),
                )
                if key in self._processed_exit_feedback:
                    continue
                self._processed_exit_feedback.add(key)
                matched_trade = update_trade_exit_result(
                    payload,
                    evidence_context=self.evidence_context,
                )
                if payload.get("is_final_exit") is False or not matched_trade:
                    continue
                pnl = float(payload.get("profit_loss", 0.0))
                if pnl < 0:
                    self.account_status.consecutive_losses += 1
                elif pnl > 0:
                    self.account_status.consecutive_losses = 0

    def _is_new_m15_candle(self) -> bool:
        df = self.bridge.fetch_ohlc_data(SYMBOLS[0], TIMEFRAME_M15, 2)
        if df.empty:
            return False
        current = df.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        if self.last_m15_candle is None or current > self.last_m15_candle:
            self.last_m15_candle = current
            return True
        return False

    def _evaluate_symbol(self, sym: str, decision_time: datetime, session_name: str | None) -> None:
        logger.debug("Evaluating symbol %s", sym)
        now_utc = decision_time
        self._insert_funnel_event(
            decision_time=decision_time,
            stage="SESSION",
            outcome="PASS",
            reason_code="SESSION_ACTIVE",
            symbol=sym,
            details=f"session={session_name}",
        )
        if is_news_blackout(sym, now_utc, self._calendar_events):
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="NEWS",
                outcome="REJECT",
                reason_code="NEWS_BLACKOUT",
                symbol=sym,
                details="high-impact event blackout window",
            )
            self._insert_risk_event(
                "NEWS_BLACKOUT", "INFO",
                f"symbol={sym} — high-impact event blackout window",
            )
            return

        sym_agents = self.agents[sym]
        regime = sym_agents["regime"].evaluate(TIMEFRAME_H1)
        if regime.regime in {"TRENDING_BULL", "TRENDING_BEAR"}:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="REGIME",
                outcome="PASS",
                reason_code=regime.regime,
                symbol=sym,
                details=f"trend_state={regime.trend_state} volatility={regime.volatility_state}",
            )
        else:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="REGIME",
                outcome="REJECT",
                reason_code=regime.regime,
                symbol=sym,
                details=f"trend_state={regime.trend_state} volatility={regime.volatility_state}",
            )
        technical = sym_agents["technical"].evaluate(regime, TIMEFRAME_M15, TIMEFRAME_H1)

        if technical is None:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="TECHNICAL",
                outcome="REJECT",
                reason_code=sym_agents["technical"].last_reason_code,
                symbol=sym,
                details=sym_agents["technical"].last_details,
            )
            return
        self._insert_funnel_event(
            decision_time=decision_time,
            stage="TECHNICAL",
            outcome="PASS",
            reason_code=technical.reason_code,
            symbol=sym,
            details=f"direction={technical.direction} rr={technical.risk_reward}",
            trade_id=technical.trade_id,
        )

        adversarial = sym_agents["adversarial"].evaluate(
            technical, self.account_status, TIMEFRAME_M15
        )
        if not adversarial.approved:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="ADVERSARIAL",
                outcome="REJECT",
                reason_code=adversarial.reason_code,
                symbol=sym,
                details=adversarial.details,
                trade_id=technical.trade_id,
            )
            self._insert_trade_proposal(
                technical,
                status="REJECTED",
                reason_code=adversarial.reason_code,
                risk_percent=0.0,
                market_regime=regime.regime,
            )
            self._insert_risk_event("PORTFOLIO_GATE", "WARN", adversarial.details, technical.trade_id)
            self.metrics.inc("trades_rejected")
            return
        self._insert_funnel_event(
            decision_time=decision_time,
            stage="ADVERSARIAL",
            outcome="PASS",
            reason_code=adversarial.reason_code,
            symbol=sym,
            details=adversarial.details,
            trade_id=technical.trade_id,
        )

        portfolio = self.portfolio_manager.evaluate(
            technical, adversarial, self.account_status,
            open_symbols=self.account_status.open_symbols,
            regime=regime,
        )

        if not portfolio.approved:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="PORTFOLIO",
                outcome="REJECT",
                reason_code=portfolio.reason_code,
                symbol=sym,
                details=portfolio.details,
                trade_id=technical.trade_id,
            )
            self._insert_trade_proposal(
                technical,
                status="REJECTED",
                reason_code=portfolio.reason_code,
                risk_percent=0.0,
                market_regime=regime.regime,
            )
            self._insert_risk_event("PORTFOLIO_GATE", "WARN", portfolio.details, technical.trade_id)
            self.metrics.inc("trades_rejected")
            return
        self._insert_funnel_event(
            decision_time=decision_time,
            stage="PORTFOLIO",
            outcome="PASS",
            reason_code=portfolio.reason_code,
            symbol=sym,
            details=portfolio.details,
            trade_id=technical.trade_id,
        )

        risk = self.hard_risk.validate(self.account_status, portfolio.final_risk_percent)
        if not risk.approved:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="HARD_RISK",
                outcome="REJECT",
                reason_code=risk.reason_code,
                symbol=sym,
                details=risk.details,
                trade_id=technical.trade_id,
            )
            self._insert_trade_proposal(
                technical,
                status="REJECTED",
                reason_code=risk.reason_code,
                risk_percent=0.0,
                market_regime=regime.regime,
            )
            self._insert_risk_event("HARD_RISK", "BLOCK", risk.details, technical.trade_id)
            self.metrics.inc("risk_blocks")
            self.metrics.inc("trades_rejected")
            return
        self._insert_funnel_event(
            decision_time=decision_time,
            stage="HARD_RISK",
            outcome="PASS",
            reason_code=risk.reason_code,
            symbol=sym,
            details=risk.details,
            trade_id=technical.trade_id,
        )

        # ML ranker gate — only active once a model has been trained.
        active_session = get_active_session(now_utc)
        is_london = 1 if active_session == "london" else 0
        is_ny = 1 if active_session == "newyork" else 0
        rate_diff = self._rate_diffs.get(sym, 0.0)
        ranker_features = {
            "regime_confidence": regime.confidence,
            "rsi": technical.rsi_at_entry,
            "atr_ratio": regime.atr_ratio,
            "spread_pips": technical.spread_entry,
            "is_london_session": float(is_london),
            "is_newyork_session": float(is_ny),
            "rate_differential": rate_diff,
            "stop_pips": technical.stop_pips,
            "risk_reward": technical.risk_reward,
            "direction_buy": 1.0 if technical.direction == "BUY" else 0.0,
            "rsi_slope": technical.rsi_slope,
        }
        ranker_prob = self.ranker.predict_proba(ranker_features)
        if not self.ranker_loaded:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="ML_RANKER",
                outcome="BYPASS",
                reason_code="ML_RANKER_MODEL_UNAVAILABLE",
                symbol=sym,
                details="bypass_untrained_model",
                trade_id=technical.trade_id,
            )
            self._insert_risk_event(
                "ML_RANKER",
                "INFO",
                "bypass_untrained_model",
                technical.trade_id,
            )
        elif ranker_prob < self.predict_threshold:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="ML_RANKER",
                outcome="REJECT",
                reason_code="ML_RANKER_LOW_PROB",
                symbol=sym,
                details=f"prob={ranker_prob:.3f} threshold={self.predict_threshold}",
                trade_id=technical.trade_id,
            )
            self._insert_trade_proposal(
                technical,
                status="REJECTED",
                reason_code="ML_RANKER_LOW_PROB",
                risk_percent=0.0,
                market_regime=regime.regime,
            )
            self._insert_risk_event(
                "ML_RANKER", "INFO",
                f"prob={ranker_prob:.3f} < threshold={self.predict_threshold}",
                technical.trade_id,
            )
            self.metrics.inc("trades_rejected")
            return
        else:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="ML_RANKER",
                outcome="PASS",
                reason_code="ML_RANKER_APPROVED",
                symbol=sym,
                details=f"prob={ranker_prob:.3f} threshold={self.predict_threshold}",
                trade_id=technical.trade_id,
            )

        # AI Probability TP Scaling
        # If the ranker outputs high confidence and we are in a strong trend, stretch the TP multiplier by 1.5x
        if ranker_prob >= 0.70 and regime.trend_state in {"BULLISH", "BEARISH"}:
            import dataclasses
            technical = dataclasses.replace(
                technical,
                take_profit_pips=round(technical.take_profit_pips * 1.5, 2)
            )
            logger.info("AI Probability Scaling applied: TP stretched 1.5x for trade %s (prob=%.3f, trend=%s)", 
                        technical.trade_id, ranker_prob, regime.trend_state)

        # Apply loss-streak throttle from hard risk engine.
        final_risk = round(portfolio.final_risk_percent * risk.risk_throttle_multiplier, 8)

        feasibility = self._preserve_10_pre_route_feasibility(
            technical.symbol,
            final_risk,
            technical.stop_pips,
        )
        if feasibility is not None and (not feasibility.can_assess or not feasibility.approved):
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="PRE_ROUTE_FEASIBILITY",
                outcome="REJECT",
                reason_code=feasibility.reason_code,
                symbol=sym,
                details=feasibility.details,
                trade_id=technical.trade_id,
            )
            self._insert_trade_proposal(
                technical,
                status="REJECTED",
                reason_code=feasibility.reason_code,
                risk_percent=0.0,
                market_regime=regime.regime,
            )
            self._insert_risk_event(
                "PRE_ROUTE_FEASIBILITY",
                "WARN",
                _format_preserve_10_preroute_event(self.policy, feasibility),
                technical.trade_id,
            )
            self.metrics.inc("trades_rejected")
            return
        if feasibility is not None:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="PRE_ROUTE_FEASIBILITY",
                outcome="PASS",
                reason_code=feasibility.reason_code,
                symbol=sym,
                details=feasibility.details,
                trade_id=technical.trade_id,
            )

        payload = technical_signal_to_payload(technical, final_risk)
        try:
            self.router.send(payload)
        except SignalRouteError as exc:
            self._insert_funnel_event(
                decision_time=decision_time,
                stage="ROUTER",
                outcome="REJECT",
                reason_code="ROUTER_SEND_UNCERTAIN" if exc.pending_written else "ROUTER_SEND_FAILED",
                symbol=sym,
                details=str(exc),
                trade_id=technical.trade_id,
            )
            if self.policy["MODE_ID"] != "preserve_10":
                raise
            if exc.pending_written:
                self._insert_trade_proposal(
                    technical,
                    status="EXECUTION_UNCERTAIN",
                    reason_code="ROUTER_SEND_UNCERTAIN",
                    risk_percent=final_risk,
                    market_regime=regime.regime,
                    regime_confidence=regime.confidence,
                    atr_ratio=regime.atr_ratio,
                    is_london_session=is_london,
                    is_newyork_session=is_ny,
                    rate_differential=rate_diff,
                )
                self._fail_closed_preserve_10_bridge(
                    f"bridge publish uncertainty for trade_id={technical.trade_id}"
                )
                self._insert_risk_event(
                    "PRESERVE_10_BRIDGE_UNCERTAIN",
                    "BLOCK",
                    str(exc),
                    technical.trade_id,
                )
                return

            self._insert_trade_proposal(
                technical,
                status="REJECTED",
                reason_code="ROUTER_SEND_FAILED",
                risk_percent=0.0,
                market_regime=regime.regime,
            )
            self._insert_risk_event(
                "PRESERVE_10_BRIDGE_FAILURE",
                "BLOCK",
                str(exc),
                technical.trade_id,
            )
            self.metrics.inc("trades_rejected")
            return

        self._insert_funnel_event(
            decision_time=decision_time,
            stage="ROUTER",
            outcome="ROUTED",
            reason_code="ROUTED_TO_MT5",
            symbol=sym,
            details=f"risk_percent={final_risk:.8f}",
            trade_id=technical.trade_id,
        )

        self._insert_trade_proposal(
            technical,
            status="PENDING",
            reason_code="ROUTED_TO_MT5",
            risk_percent=final_risk,
            market_regime=regime.regime,
            regime_confidence=regime.confidence,
            atr_ratio=regime.atr_ratio,
            is_london_session=is_london,
            is_newyork_session=is_ny,
            rate_differential=rate_diff,
        )
        self.metrics.inc("trades_routed")

    def _decision_cycle(self) -> None:
        logger.info("Starting decision cycle for %d symbols", len(SYMBOLS))
        with self.tracer.start_as_current_span("decision_cycle"):
            now_utc = datetime.now(timezone.utc)
            if not is_tradeable_session(now_utc):
                for sym in SYMBOLS:
                    self._insert_funnel_event(
                        decision_time=now_utc,
                        stage="SESSION",
                        outcome="REJECT",
                        reason_code="SESSION_INACTIVE",
                        symbol=sym,
                        details=f"hour={now_utc.hour} session={get_active_session(now_utc)}",
                    )
                self._insert_risk_event(
                    "SESSION_INACTIVE", "INFO",
                    f"hour={now_utc.hour} UTC — outside London/NY session",
                )
                return

            # Remove only stale/orphaned pending artifacts; keep valid pending signals.
            cleanup = self.router.cleanup_stale(max_age_seconds=600)
            if self.policy["MODE_ID"] == "preserve_10":
                stale_trade_ids = tuple(cleanup.stale_pending_trade_ids)
                orphan_trade_ids = tuple(cleanup.orphan_lock_trade_ids)
                for trade_id in stale_trade_ids:
                    mark_trade_execution_uncertain(trade_id, "ROUTER_PENDING_UNCERTAIN")
                    self._insert_risk_event(
                        "PRESERVE_10_BRIDGE_UNCERTAIN",
                        "BLOCK",
                        "stale pending signal quarantined; execution truth is uncertain",
                        trade_id,
                    )
                for trade_id in orphan_trade_ids:
                    mark_trade_execution_uncertain(trade_id, "ROUTER_LOCK_UNCERTAIN")
                    self._insert_risk_event(
                        "PRESERVE_10_BRIDGE_UNCERTAIN",
                        "BLOCK",
                        "orphan router lock quarantined; execution truth is uncertain",
                        trade_id,
                    )
                if stale_trade_ids or orphan_trade_ids:
                    self._fail_closed_preserve_10_bridge(
                        "preserve-first bridge uncertainty detected during router housekeeping"
                    )
                    return
            else:
                for trade_id in cleanup.stale_pending_trade_ids:
                    mark_trade_expired(trade_id, "ROUTER_PENDING_EXPIRED")
                    self._insert_risk_event(
                        "ROUTER_HOUSEKEEPING",
                        "WARN",
                        "stale pending signal expired by TTL",
                        trade_id,
                    )

            session_name = get_active_session(now_utc)
            for sym in SYMBOLS:
                self._evaluate_symbol(sym, now_utc, session_name)

    def run(self, mode: str, iterations: int = 0) -> None:
        count = 0
        while True:
            self._update_account_state()

            if self.account_status.is_stale(max_age_seconds=180):
                self.account_status.is_trading_halted = True
                if not self._stale_episode_active:
                    self._stale_episode_active = True
                    last_upd = self.account_status.updated_at.isoformat()
                    now_str = datetime.now(timezone.utc).isoformat()
                    msg = f"Account state stale. Last update: {last_upd}, Current: {now_str}"
                    self._insert_risk_event("STATE_STALE", "BLOCK", msg)
                    logger.warning(msg)
                    self.metrics.inc("state_stale")
            elif self._stale_episode_active:
                self._stale_episode_active = False
                self._insert_risk_event("STATE_RECOVERED", "INFO", "Account state refreshed; trading unhalted")
                logger.info("Account state recovered after stale episode.")

            self._consume_feedback()

            if self._is_new_m15_candle() and not self.account_status.is_trading_halted:
                self._decision_cycle()

            if self.mock_feedback is not None:
                simulated = self.mock_feedback.process_pending(account_status=self.account_status)
                if simulated > 0:
                    self._consume_feedback()
                    self._update_account_state()
                    self.mock_feedback.clear_account_snapshot()

            insert_account_metrics(self.account_status, evidence_context=self.evidence_context)
            self.metrics.inc("decision_cycles")

            count += 1
            if mode == "smoke":
                if count >= 1:
                    break
            elif iterations > 0 and count >= iterations:
                break

            time.sleep(5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FX AI Engine")
    parser.add_argument("--mode", choices=["smoke", "demo"], default="smoke")
    parser.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Optional loop iterations for demo mode. 0 means run until stopped.",
    )
    return parser.parse_args()


def main() -> int:
    configure_logging()
    load_runtime_env()

    initialize_schema()
    migrate_phase8_columns()
    migrate_add_risk_events()
    migrate_add_decision_funnel_events()
    migrate_add_ml_feature_columns()
    migrate_add_restart_state_columns()
    migrate_add_evidence_partition_columns()

    use_mock = os.getenv("USE_MT5_MOCK") == "1"
    if use_mock:
        creds = None
    else:
        try:
            creds = load_mt5_credentials_from_env()
        except CredentialsError as exc:
            logger.error("Credential error: %s", exc)
            return 1

    tracer = init_tracing(os.getenv("OTEL_SERVICE_NAME", "fx_ai_engine"))
    metrics = init_metrics()

    bridge = MT5Connection(
        creds.login if creds else 0,
        creds.password if creds else "",
        creds.server if creds else "",
    )
    if not bridge.connect():
        logger.error("MT5 connection failed: %s", bridge.last_error)
        return 2

    try:
        policy = get_policy_config()
        startup_evidence_context = build_runtime_evidence_context(
            policy,
            use_mock=use_mock,
            login=getattr(bridge, "login", 0),
            server=getattr(bridge, "server", ""),
        )
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

        args = parse_args()

        try:
            engine = Engine(bridge, tracer, metrics, use_mock=use_mock)
        except RuntimeError as exc:
            logger.error("Engine initialization failed: %s", exc)
            return 2
        engine.run(mode=args.mode, iterations=args.iterations)
    finally:
        bridge.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
