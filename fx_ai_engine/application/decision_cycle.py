"""One decision cycle: calendar and session gates, router housekeeping, per-symbol evaluation.

The order of stages is the SRS decision flow: Regime -> Technical -> Adversarial
-> Portfolio Manager -> Hard Risk Engine -> [ML shadow/canary] -> reservation
-> Signal Router. Functions take the engine as their first argument.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Mapping

from application.market_data_service import SYMBOLS
from application.routing_service import ReservationRejected
from bridge.signal_router import SignalRouteError
from core.data_quality.freshness import evaluate_calendar_freshness
from core.filters.calendar_filter import is_news_blackout
from core.filters.session_filter import get_active_session, is_tradeable_session
from core.schemas import technical_signal_to_payload
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15
from database.db import mark_trade_execution_uncertain
from ml.meta_labeler.shadow_runtime import evaluate_canary_decision, preserve_primary_route_decision

# Same logger name as before the split so log routing is unchanged.
logger = logging.getLogger("fx_ai_engine.main")


def _policy_evidence_suffix(policy: Mapping[str, object]) -> str:
    return f"[mode={policy['MODE_ID']} evidence={policy['EVIDENCE_LABEL']}]"


def _with_policy_evidence(policy: Mapping[str, object], details: str) -> str:
    return f"{details} {_policy_evidence_suffix(policy)}"


def _normalize_pre_route_feasibility_details(feasibility) -> str:
    details = str(feasibility.details)
    if not feasibility.can_assess:
        return details.replace(
            "symbol execution contract unavailable",
            "contract data is unavailable",
        )
    return f"{details}; trade blocked before MT5 routing"


def _format_pre_route_feasibility_event(
    policy: Mapping[str, object],
    feasibility,
) -> str:
    outcome = "blocked before MT5 routing" if not feasibility.can_assess else "refused before MT5 routing"
    mode_label = str(policy.get("MODE_LABEL") or policy.get("MODE_ID") or "Policy")
    return _with_policy_evidence(
        policy,
        f"{mode_label} pre-route feasibility {outcome}: {_normalize_pre_route_feasibility_details(feasibility)}",
    )


def _format_strategic_risk_event(
    policy: Mapping[str, object],
    eligibility,
) -> str:
    outcome = "blocked before downstream routing checks" if not eligibility.can_assess else "rejected as non-deployable"
    mode_label = str(policy.get("MODE_LABEL") or policy.get("MODE_ID") or "Policy")
    return _with_policy_evidence(
        policy,
        f"{mode_label} strategic-risk eligibility {outcome}: {eligibility.details}",
    )


def evaluate_symbol(engine, sym: str, decision_time: datetime, session_name: str | None) -> None:
    logger.debug("Evaluating symbol %s", sym)
    now_utc = decision_time
    engine._insert_funnel_event(
        decision_time=decision_time,
        stage="SESSION",
        outcome="PASS",
        reason_code="SESSION_ACTIVE",
        symbol=sym,
        details=f"session={session_name}",
    )
    if is_news_blackout(sym, now_utc, engine._calendar_events):
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="NEWS",
            outcome="REJECT",
            reason_code="NEWS_BLACKOUT",
            symbol=sym,
            details="high-impact event blackout window",
        )
        engine._insert_risk_event(
            "NEWS_BLACKOUT", "INFO",
            f"symbol={sym} — high-impact event blackout window",
        )
        return

    sym_agents = engine.agents[sym]
    regime = sym_agents["regime"].evaluate(TIMEFRAME_H1)
    if regime.regime in {"TRENDING_BULL", "TRENDING_BEAR"}:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="REGIME",
            outcome="PASS",
            reason_code=regime.reason_code,
            symbol=sym,
            details=sym_agents["regime"].last_details,
        )
    else:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="REGIME",
            outcome="REJECT",
            reason_code=regime.reason_code,
            symbol=sym,
            details=sym_agents["regime"].last_details,
        )
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="TECHNICAL",
            outcome="SKIP",
            reason_code="TECH_SKIPPED_REGIME_REJECTED",
            symbol=sym,
            details=(
                f"regime={regime.regime} regime_reason={regime.reason_code} "
                f"trend_state={regime.trend_state}"
            ),
        )
        return
    technical = sym_agents["technical"].evaluate(regime, TIMEFRAME_M15, TIMEFRAME_H1)

    if technical is None:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="TECHNICAL",
            outcome="REJECT",
            reason_code=sym_agents["technical"].last_reason_code,
            symbol=sym,
            details=sym_agents["technical"].last_details,
        )
        return
    engine._insert_funnel_event(
        decision_time=decision_time,
        stage="TECHNICAL",
        outcome="PASS",
        reason_code=technical.reason_code,
        symbol=sym,
        details=sym_agents["technical"].last_details,
        trade_id=technical.trade_id,
    )

    strategic = engine._strategic_risk_eligibility(
        technical.symbol,
        technical.stop_pips,
    )
    if strategic is not None and (not strategic.can_assess or not strategic.approved):
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="STRATEGIC_RISK",
            outcome="REJECT",
            reason_code=strategic.reason_code,
            symbol=sym,
            details=strategic.details,
            trade_id=technical.trade_id,
        )
        engine._insert_trade_proposal(
            technical,
            status="REJECTED",
            reason_code=strategic.reason_code,
            risk_percent=0.0,
            market_regime=regime.regime,
        )
        engine._insert_risk_event(
            "STRATEGIC_RISK",
            "WARN",
            _format_strategic_risk_event(engine.policy, strategic),
            technical.trade_id,
        )
        engine.metrics.inc("trades_rejected")
        return
    if strategic is not None:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="STRATEGIC_RISK",
            outcome="PASS",
            reason_code=strategic.reason_code,
            symbol=sym,
            details=strategic.details,
            trade_id=technical.trade_id,
        )

    adversarial = sym_agents["adversarial"].evaluate(
        technical, engine.account_status, TIMEFRAME_M15
    )
    if not adversarial.approved:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="ADVERSARIAL",
            outcome="REJECT",
            reason_code=adversarial.reason_code,
            symbol=sym,
            details=adversarial.details,
            trade_id=technical.trade_id,
        )
        engine._insert_trade_proposal(
            technical,
            status="REJECTED",
            reason_code=adversarial.reason_code,
            risk_percent=0.0,
            market_regime=regime.regime,
        )
        engine._insert_risk_event("PORTFOLIO_GATE", "WARN", adversarial.details, technical.trade_id)
        engine.metrics.inc("trades_rejected")
        return
    engine._insert_funnel_event(
        decision_time=decision_time,
        stage="ADVERSARIAL",
        outcome="PASS",
        reason_code=adversarial.reason_code,
        symbol=sym,
        details=adversarial.details,
        trade_id=technical.trade_id,
    )

    portfolio = engine.portfolio_manager.evaluate(
        technical, adversarial, engine.account_status,
        open_symbols=engine.account_status.open_symbols,
        regime=regime,
    )

    if not portfolio.approved:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="PORTFOLIO",
            outcome="REJECT",
            reason_code=portfolio.reason_code,
            symbol=sym,
            details=portfolio.details,
            trade_id=technical.trade_id,
        )
        engine._insert_trade_proposal(
            technical,
            status="REJECTED",
            reason_code=portfolio.reason_code,
            risk_percent=0.0,
            market_regime=regime.regime,
        )
        engine._insert_risk_event("PORTFOLIO_GATE", "WARN", portfolio.details, technical.trade_id)
        engine.metrics.inc("trades_rejected")
        return
    engine._insert_funnel_event(
        decision_time=decision_time,
        stage="PORTFOLIO",
        outcome="PASS",
        reason_code=portfolio.reason_code,
        symbol=sym,
        details=portfolio.details,
        trade_id=technical.trade_id,
    )

    risk = engine.hard_risk.validate(engine.account_status, portfolio.final_risk_percent)
    if not risk.approved:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="HARD_RISK",
            outcome="REJECT",
            reason_code=risk.reason_code,
            symbol=sym,
            details=risk.details,
            trade_id=technical.trade_id,
        )
        engine._insert_trade_proposal(
            technical,
            status="REJECTED",
            reason_code=risk.reason_code,
            risk_percent=0.0,
            market_regime=regime.regime,
        )
        engine._insert_risk_event("HARD_RISK", "BLOCK", risk.details, technical.trade_id)
        engine.metrics.inc("risk_blocks")
        engine.metrics.inc("trades_rejected")
        return
    engine._insert_funnel_event(
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
    rate_diff = engine._rate_diffs.get(sym, 0.0)
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
    ranker_prob = engine.ranker.predict_proba(ranker_features)
    primary_gate_route = not (engine.ranker_loaded and ranker_prob < engine.predict_threshold)

    shadow_decision = None
    if engine.shadow_runtime.enabled:
        shadow_decision = engine.shadow_runtime.evaluate(
            symbol=sym,
            trade_id=technical.trade_id,
            regime_label=regime.regime,
            decision_time=decision_time,
            technical_timestamp_utc=technical.timestamp_utc,
        )
        primary_gate_route = preserve_primary_route_decision(
            primary_gate_route,
            shadow_decision.outcome,
        )
        engine._insert_ml_shadow_event(
            decision_time=decision_time,
            symbol=sym,
            trade_id=technical.trade_id,
            stage="POST_HARD_RISK",
            primary_gate_outcome="ROUTE" if primary_gate_route else "REJECT",
            shadow_decision=shadow_decision,
        )

    canary_decision = evaluate_canary_decision(
        engine.canary_config,
        stage="POST_HARD_RISK",
        shadow_enabled=engine.shadow_runtime.enabled,
        shadow_outcome=(shadow_decision.outcome if shadow_decision is not None else "DISABLED"),
        primary_gate_route=primary_gate_route,
    )
    if engine.canary_config.enabled or engine.canary_config.kill_switch:
        canary_outcome = "REJECT" if canary_decision.block_route else ("PASS" if canary_decision.enforced else "BYPASS")
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="ML_CANARY",
            outcome=canary_outcome,
            reason_code=canary_decision.reason_code,
            symbol=sym,
            details=canary_decision.details,
            trade_id=technical.trade_id,
        )
    if canary_decision.block_route:
        engine._insert_trade_proposal(
            technical,
            status="REJECTED",
            reason_code=canary_decision.reason_code,
            risk_percent=0.0,
            market_regime=regime.regime,
        )
        engine._insert_risk_event(
            "ML_CANARY",
            "BLOCK",
            canary_decision.details,
            technical.trade_id,
        )
        engine.metrics.inc("trades_rejected")
        return

    if not engine.ranker_loaded:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="ML_RANKER",
            outcome="BYPASS",
            reason_code="ML_RANKER_MODEL_UNAVAILABLE",
            symbol=sym,
            details="bypass_untrained_model",
            trade_id=technical.trade_id,
        )
        engine._insert_risk_event(
            "ML_RANKER",
            "INFO",
            "bypass_untrained_model",
            technical.trade_id,
        )
    elif not primary_gate_route:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="ML_RANKER",
            outcome="REJECT",
            reason_code="ML_RANKER_LOW_PROB",
            symbol=sym,
            details=f"prob={ranker_prob:.3f} threshold={engine.predict_threshold}",
            trade_id=technical.trade_id,
        )
        engine._insert_trade_proposal(
            technical,
            status="REJECTED",
            reason_code="ML_RANKER_LOW_PROB",
            risk_percent=0.0,
            market_regime=regime.regime,
        )
        engine._insert_risk_event(
            "ML_RANKER", "INFO",
            f"prob={ranker_prob:.3f} < threshold={engine.predict_threshold}",
            technical.trade_id,
        )
        engine.metrics.inc("trades_rejected")
        return
    else:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="ML_RANKER",
            outcome="PASS",
            reason_code="ML_RANKER_APPROVED",
            symbol=sym,
            details=f"prob={ranker_prob:.3f} threshold={engine.predict_threshold}",
            trade_id=technical.trade_id,
        )

    # Apply loss-streak throttle from hard risk engine.
    final_risk = round(portfolio.final_risk_percent * risk.risk_throttle_multiplier, 8)

    feasibility = engine._pre_route_feasibility(
        technical.symbol,
        final_risk,
        technical.stop_pips,
    )
    if feasibility is not None and (not feasibility.can_assess or not feasibility.approved):
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="PRE_ROUTE_FEASIBILITY",
            outcome="REJECT",
            reason_code=feasibility.reason_code,
            symbol=sym,
            details=feasibility.details,
            trade_id=technical.trade_id,
        )
        engine._insert_trade_proposal(
            technical,
            status="REJECTED",
            reason_code=feasibility.reason_code,
            risk_percent=0.0,
            market_regime=regime.regime,
        )
        engine._insert_risk_event(
            "PRE_ROUTE_FEASIBILITY",
            "WARN",
            _format_pre_route_feasibility_event(engine.policy, feasibility),
            technical.trade_id,
        )
        engine.metrics.inc("trades_rejected")
        return
    if feasibility is not None:
        engine._insert_funnel_event(
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
        engine.routing_service.reserve_and_route(payload, account_status=engine.account_status)
    except ReservationRejected as exc:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="RESERVATION",
            outcome="REJECT",
            reason_code=str(exc),
            symbol=sym,
            details="portfolio capacity unavailable after counting pending reservations",
            trade_id=technical.trade_id,
        )
        engine._insert_trade_proposal(
            technical,
            status="REJECTED",
            reason_code=str(exc),
            risk_percent=0.0,
            market_regime=regime.regime,
        )
        engine._insert_risk_event("PORTFOLIO_RESERVATION", "BLOCK", str(exc), technical.trade_id)
        engine.metrics.inc("trades_rejected")
        return
    except SignalRouteError as exc:
        engine._insert_funnel_event(
            decision_time=decision_time,
            stage="ROUTER",
            outcome="REJECT",
            reason_code="ROUTER_SEND_UNCERTAIN" if exc.pending_written else "ROUTER_SEND_FAILED",
            symbol=sym,
            details=str(exc),
            trade_id=technical.trade_id,
        )
        if engine.policy["MODE_ID"] != "preserve_10":
            raise
        if exc.pending_written:
            engine._insert_trade_proposal(
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
            engine._fail_closed_preserve_10_bridge(
                f"bridge publish uncertainty for trade_id={technical.trade_id}"
            )
            engine._insert_risk_event(
                "PRESERVE_10_BRIDGE_UNCERTAIN",
                "BLOCK",
                str(exc),
                technical.trade_id,
            )
            return

        engine._insert_trade_proposal(
            technical,
            status="REJECTED",
            reason_code="ROUTER_SEND_FAILED",
            risk_percent=0.0,
            market_regime=regime.regime,
        )
        engine._insert_risk_event(
            "PRESERVE_10_BRIDGE_FAILURE",
            "BLOCK",
            str(exc),
            technical.trade_id,
        )
        engine.metrics.inc("trades_rejected")
        return

    engine._insert_funnel_event(
        decision_time=decision_time,
        stage="ROUTER",
        outcome="ROUTED",
        reason_code="ROUTED_TO_MT5",
        symbol=sym,
        details=f"risk_percent={final_risk:.8f}",
        trade_id=technical.trade_id,
    )

    engine._insert_trade_proposal(
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
    engine.metrics.inc("trades_routed")


def run_decision_cycle(engine) -> None:
    logger.info("Starting decision cycle for %d symbols", len(SYMBOLS))
    with engine.tracer.start_as_current_span("decision_cycle"):
        now_utc = datetime.now(timezone.utc)
        engine._calendar_quality = evaluate_calendar_freshness(
            engine._calendar_events,
            now_utc=now_utc,
        )
        if not engine._calendar_quality.approved:
            engine._insert_risk_event(
                engine._calendar_quality.reason_code,
                "BLOCK",
                engine._calendar_quality.details,
            )
            return
        if not is_tradeable_session(now_utc):
            for sym in SYMBOLS:
                engine._insert_funnel_event(
                    decision_time=now_utc,
                    stage="SESSION",
                    outcome="REJECT",
                    reason_code="SESSION_INACTIVE",
                    symbol=sym,
                    details=f"hour={now_utc.hour} session={get_active_session(now_utc)}",
                )
            engine._insert_risk_event(
                "SESSION_INACTIVE", "INFO",
                f"hour={now_utc.hour} UTC — outside London/NY session",
            )
            return

        # Remove only stale/orphaned pending artifacts; keep valid pending signals.
        cleanup = engine.router.cleanup_stale(max_age_seconds=600)
        if engine.policy["MODE_ID"] == "preserve_10":
            stale_trade_ids = tuple(cleanup.stale_pending_trade_ids)
            orphan_trade_ids = tuple(cleanup.orphan_lock_trade_ids)
            for trade_id in stale_trade_ids:
                mark_trade_execution_uncertain(trade_id, "ROUTER_PENDING_UNCERTAIN")
                engine._insert_risk_event(
                    "PRESERVE_10_BRIDGE_UNCERTAIN",
                    "BLOCK",
                    "stale pending signal quarantined; execution truth is uncertain",
                    trade_id,
                )
            for trade_id in orphan_trade_ids:
                mark_trade_execution_uncertain(trade_id, "ROUTER_LOCK_UNCERTAIN")
                engine._insert_risk_event(
                    "PRESERVE_10_BRIDGE_UNCERTAIN",
                    "BLOCK",
                    "orphan router lock quarantined; execution truth is uncertain",
                    trade_id,
                )
            if stale_trade_ids or orphan_trade_ids:
                engine._fail_closed_preserve_10_bridge(
                    "preserve-first bridge uncertainty detected during router housekeeping"
                )
                return
        else:
            stale_trade_ids = tuple(cleanup.stale_pending_trade_ids)
            orphan_trade_ids = tuple(cleanup.orphan_lock_trade_ids)
            for trade_id in stale_trade_ids:
                mark_trade_execution_uncertain(trade_id, "ROUTER_PENDING_UNCERTAIN")
                engine._insert_risk_event(
                    "BRIDGE_EXECUTION_UNCERTAIN",
                    "BLOCK",
                    "stale pending signal quarantined; execution truth is uncertain",
                    trade_id,
                )
            for trade_id in orphan_trade_ids:
                mark_trade_execution_uncertain(trade_id, "ROUTER_LOCK_UNCERTAIN")
                engine._insert_risk_event(
                    "BRIDGE_EXECUTION_UNCERTAIN",
                    "BLOCK",
                    "orphan router lock quarantined; execution truth is uncertain",
                    trade_id,
                )
            if stale_trade_ids or orphan_trade_ids:
                engine._fail_closed_bridge_uncertainty(
                    "bridge uncertainty detected during router housekeeping"
                )
                return

        session_name = get_active_session(now_utc)
        for sym in SYMBOLS:
            engine._evaluate_symbol(sym, now_utc, session_name)
