"""Engine scheduling loop: one iteration per poll, in the fixed order below."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from core.health import update_health_status
from core.mt5_bridge import MT5Connection

# Same logger name as before the split so log routing is unchanged.
logger = logging.getLogger("fx_ai_engine.main")


def _demo_account_state(bridge: MT5Connection, *, use_mock: bool) -> bool | None:
    """True: verified demo. False: the terminal reports a live account. None: unverified."""
    if use_mock:
        return True
    try:
        verified = bridge.is_demo_account()
    except Exception:
        logger.exception("Unable to verify MT5 account type")
        return None
    return verified if verified in (True, False) else None


def _demo_account_verified(bridge: MT5Connection, *, use_mock: bool) -> bool:
    """Demo mode must never operate against a live or unverified account."""
    return _demo_account_state(bridge, use_mock=use_mock) is True


def run(engine, mode: str, iterations: int = 0) -> None:
    count = 0
    while True:
        # Demo mode routes entries only on a verified demo account. A live account
        # stops the loop before any processing. An unverified one (stale or unreadable
        # snapshot, also a host guard BLOCK) blocks entries but keeps feedback, exits,
        # closure recovery, the host guard and the outage alert running.
        demo_unverified = False
        if mode == "demo":
            account_state = _demo_account_state(engine.bridge, use_mock=engine.use_mock)
            if account_state is False:
                logger.error("Connected MT5 account is not a demo account; stopping before processing or routing signals")
                engine._fail_closed_bridge_uncertainty("demo account unverified or changed")
                try:
                    engine._poll_host_guard(datetime.now(timezone.utc), force=True)
                except Exception:
                    logger.exception("Host guard could not be evaluated before the demo stop")
                return
            demo_unverified = account_state is None
            if demo_unverified:
                if not getattr(engine, "_demo_unverified", False):
                    logger.error("Demo account unverified; new entries blocked, feedback and exits still processed")
                    # Record the host guard BLOCK at once (a stale snapshot is one).
                    try:
                        engine._poll_host_guard(datetime.now(timezone.utc), force=True)
                    except Exception:
                        logger.exception("Host guard could not be evaluated for the unverified demo account")
                engine._fail_closed_bridge_uncertainty("demo account unverified or changed")
            elif getattr(engine, "_demo_unverified", False):
                logger.info("Demo account verified again")
            engine._demo_unverified = demo_unverified
        update_health_status(
            status="running",
            account_balance=engine.account_status.balance,
            is_trading_halted=engine.account_status.is_trading_halted,
            open_positions=engine.account_status.open_positions_count,
            mode=mode
        )
        engine._consume_feedback()
        engine._recover_missing_trade_closures_from_broker()
        engine._update_account_state()
        if demo_unverified:  # the account update must not lift the demo entry block
            engine._fail_closed_bridge_uncertainty("demo account unverified or changed")

        engine._enforce_execution_feedback_integrity()

        if engine.account_status.is_stale(max_age_seconds=180):
            engine.account_status.is_trading_halted = True
            if not engine._stale_episode_active:
                engine._stale_episode_active = True
                last_upd = engine.account_status.updated_at.isoformat()
                now_str = datetime.now(timezone.utc).isoformat()
                msg = f"Account state stale. Last update: {last_upd}, Current: {now_str}"
                engine._insert_risk_event("STATE_STALE", "BLOCK", msg)
                logger.warning(msg)
                engine.metrics.inc("state_stale")
        elif engine._stale_episode_active:
            engine._stale_episode_active = False
            engine._insert_risk_event("STATE_RECOVERED", "INFO", "Account state refreshed; trading unhalted")
            logger.info("Account state recovered after stale episode.")

        # Host tolerance: cheap when not due (operating-model doc, section 6).
        now_utc = datetime.now(timezone.utc)
        engine._poll_host_guard(now_utc)
        engine._poll_outage_monitor(now_utc)

        if engine._is_new_m15_candle() and not engine.account_status.is_trading_halted and not demo_unverified:
            engine._decision_cycle()

        if engine.mock_feedback is not None:
            simulated = engine.mock_feedback.process_pending(account_status=engine.account_status)
            if simulated > 0:
                engine._consume_feedback()
                engine._recover_missing_trade_closures_from_broker()
                engine._update_account_state()
                engine.mock_feedback.clear_account_snapshot()

        engine.repository.insert_account_metrics(engine.account_status, evidence_context=engine.evidence_context)
        engine._consecutive_losses_dirty = False
        engine.metrics.inc("decision_cycles")

        count += 1
        if mode == "smoke":
            if count >= 1:
                break
        elif iterations > 0 and count >= iterations:
            break

        time.sleep(5)


RECONCILE_ONLY_REASON = "start refused by a policy gate; reconciliation only"


def reconcile_once(engine) -> None:
    """HT-009A: one reconciliation pass for a demo start a policy gate refused.

    Applies execution feedback, final exit files and broker closure recovery, and
    records the host guard, so a broker-side TP or SL during downtime closes the
    trade and its reservation even when the start is refused. Entries stay blocked
    throughout: no decision cycle, no mock fill simulation, nothing is routed, and
    the forced halt is not persisted as account metrics.
    """
    engine._fail_closed_bridge_uncertainty(RECONCILE_ONLY_REASON)
    engine._consume_feedback()
    engine._recover_missing_trade_closures_from_broker()
    engine._update_account_state()
    engine._fail_closed_bridge_uncertainty(RECONCILE_ONLY_REASON)
    engine._enforce_execution_feedback_integrity()
    try:
        engine._poll_host_guard(datetime.now(timezone.utc), force=True)
    except Exception:
        logger.exception("Host guard could not be evaluated during the reconciliation-only pass")
