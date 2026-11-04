"""Engine scheduling loop: one iteration per poll, in the fixed order below."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from core.health import update_health_status
from core.mt5_bridge import MT5Connection

# Same logger name as before the split so log routing is unchanged.
logger = logging.getLogger("fx_ai_engine.main")


def _demo_account_verified(bridge: MT5Connection, *, use_mock: bool) -> bool:
    """Demo mode must never operate against a live or unverified account."""
    if use_mock:
        return True
    try:
        return bridge.is_demo_account() is True
    except Exception:
        logger.exception("Unable to verify MT5 account type")
        return False


def run(engine, mode: str, iterations: int = 0) -> None:
    count = 0
    while True:
        if mode == "demo" and not _demo_account_verified(engine.bridge, use_mock=engine.use_mock):
            logger.error("Demo account verification failed; stopping before processing or routing signals")
            engine._fail_closed_bridge_uncertainty("demo account unverified or changed")
            return
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

        if engine._is_new_m15_candle() and not engine.account_status.is_trading_halted:
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
