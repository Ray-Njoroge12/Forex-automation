"""Market and account data: closed-bar detection, account snapshots and static market context."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Mapping

from application.feedback_service import _parse_timestamp_utc
from core.data_quality.freshness import evaluate_calendar_freshness, evaluate_rate_differential_freshness
from core.filters.calendar_filter import load_calendar
from core.filters.macro_filter import load_rate_differential_metadata, load_rate_differentials
from core.state_sync import update_account_status_from_snapshot
from core.timeframes import TIMEFRAME_M15

# Same logger name as before the split so log routing is unchanged.
logger = logging.getLogger("fx_ai_engine.main")

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"]
MAX_ACCOUNT_SNAPSHOT_AGE_SECONDS = 30
LAST_M15_CANDLE_STATE_KEY = "last_m15_candle_by_symbol"


def _snapshot_age_seconds(
    snapshot: Mapping[str, object],
    *,
    now_utc: datetime,
) -> float | None:
    snapshot_time_utc = _parse_timestamp_utc(snapshot.get("timestamp"))
    if snapshot_time_utc is None:
        return None
    return (now_utc - snapshot_time_utc).total_seconds()


def _live_trade_mgmt_option_c_enabled(policy: Mapping[str, object]) -> bool:
    experiments = policy.get("EXPERIMENTS", {})
    if not isinstance(experiments, Mapping):
        return False
    cfg = experiments.get("LIVE_TRADE_MGMT_OPTION_C", {})
    return isinstance(cfg, Mapping) and bool(cfg.get("enabled", False))


def load_market_context(engine, data_dir: str) -> None:
    """Load the calendar and rate-differential files once at start-up."""
    engine._calendar_events = load_calendar(
        os.path.join(data_dir, "economic_calendar.json")
    )
    engine._calendar_quality = evaluate_calendar_freshness(engine._calendar_events)
    rate_path = os.path.join(data_dir, "rate_differentials.json")
    engine._rate_quality = evaluate_rate_differential_freshness(
        load_rate_differential_metadata(rate_path)
    )
    engine._rate_diffs = load_rate_differentials(rate_path) if engine._rate_quality.approved else {}
    if not engine._rate_quality.approved:
        logger.warning(
            "Macro modifier disabled: %s %s",
            engine._rate_quality.reason_code,
            engine._rate_quality.details,
        )


def update_account_state(engine) -> None:
    with engine.tracer.start_as_current_span("update_account_state") as span:
        now_utc = datetime.now(timezone.utc)
        snapshot = engine.feedback.read_account_snapshot()
        if snapshot is not None and snapshot.get("snapshot_source") == "mock_feedback_simulator" and not engine.use_mock:
            logger.warning("Ignoring mock-generated account snapshot while running in non-mock mode")
            snapshot = None
        if snapshot is not None:
            snapshot = dict(snapshot)
            snapshot_age_seconds = _snapshot_age_seconds(snapshot, now_utc=now_utc)
            if snapshot_age_seconds is None or snapshot_age_seconds > MAX_ACCOUNT_SNAPSHOT_AGE_SECONDS:
                logger.warning(
                    "Ignoring stale feedback account snapshot timestamp=%s age_seconds=%s max_age_seconds=%s",
                    snapshot.get("timestamp"),
                    "n/a" if snapshot_age_seconds is None else f"{snapshot_age_seconds:.1f}",
                    MAX_ACCOUNT_SNAPSHOT_AGE_SECONDS,
                )
                snapshot = None

        if snapshot is not None and engine._is_snapshot_stale_after_broker_close(snapshot):
            latest_close = engine._latest_broker_close_time_utc.isoformat() if engine._latest_broker_close_time_utc else "n/a"
            logger.info(
                "Ignoring stale feedback account snapshot timestamp=%s latest_broker_close=%s",
                snapshot.get("timestamp"),
                latest_close,
            )
            snapshot = None

        if snapshot is None:
            snapshot = engine.bridge.get_account_snapshot() or {}

        if (
            _live_trade_mgmt_option_c_enabled(engine.policy)
            and snapshot
            and not snapshot.get("error")
            and int(snapshot.get("open_positions_count", 0) or 0) > 0
            and "management_state_restored" not in snapshot
        ):
            snapshot["management_state_restored"] = False
            snapshot["management_state_error"] = (
                "management restore health unavailable from EA account_snapshot"
            )

        if engine._consecutive_losses_dirty and "consecutive_losses" not in snapshot:
            snapshot["consecutive_losses"] = engine.account_status.consecutive_losses

        persisted_state = engine.repository.get_latest_account_metric(
            evidence_stream=engine.evidence_context.evidence_stream,
            account_scope=engine.evidence_context.account_scope,
        )
        trade_ledger = engine.repository.get_open_trade_ledger(
            evidence_stream=engine.evidence_context.evidence_stream,
            account_scope=engine.evidence_context.account_scope,
        )

        update_account_status_from_snapshot(
            engine.account_status,
            snapshot,
            persisted_state=persisted_state,
            trade_ledger=trade_ledger,
            now_utc=now_utc,
        )
        if snapshot and not snapshot.get("error"):
            engine._consecutive_losses_seeded = True

        if not engine.account_status.state_reconciled:
            if engine.account_status.state_reconciliation_reason != engine._last_reconciliation_reason:
                engine._insert_risk_event(
                    "STATE_RECONCILIATION_FAILED",
                    "BLOCK",
                    engine.account_status.state_reconciliation_reason,
                )
                logger.warning(
                    "Account state reconciliation failed: %s",
                    engine.account_status.state_reconciliation_reason,
                )
            engine._last_reconciliation_reason = engine.account_status.state_reconciliation_reason
        elif engine._last_reconciliation_reason:
            engine._insert_risk_event("STATE_RECONCILED", "INFO", "Account state reconciled")
            logger.info("Account state reconciled after previous mismatch.")
            engine._last_reconciliation_reason = ""

        if engine.reset_scheduler.should_reset_daily():
            engine.account_status.daily_loss_percent = 0.0
        if engine.reset_scheduler.should_reset_weekly():
            engine.account_status.weekly_loss_percent = 0.0

        span.set_attribute("open_positions", engine.account_status.open_positions_count)
        span.set_attribute("drawdown_percent", engine.account_status.drawdown_percent)

    engine.metrics.set_gauge("open_positions", engine.account_status.open_positions_count)
    engine.metrics.set_gauge("open_risk_percent", engine.account_status.open_risk_percent)


def is_new_m15_candle(engine) -> bool:
    saw_new_candle = False
    latest_candle = engine.last_m15_candle

    for symbol in SYMBOLS:
        df = engine.bridge.fetch_ohlc_data(symbol, TIMEFRAME_M15, 2)
        if df.empty:
            continue

        current = df.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        previous = engine._last_m15_candle_by_symbol.get(symbol)
        if previous is None or current > previous:
            engine._last_m15_candle_by_symbol[symbol] = current
            saw_new_candle = True

        if latest_candle is None or current > latest_candle:
            latest_candle = current

    if latest_candle is not None:
        engine.last_m15_candle = latest_candle

    if saw_new_candle:
        engine._store_last_m15_candles()
    return saw_new_candle


def load_last_m15_candles(engine) -> dict[str, datetime]:
    """Seed the per-symbol seen-candle map so a restart does not re-run the current bar."""
    try:
        raw = engine.repository.get_engine_runtime_state(LAST_M15_CANDLE_STATE_KEY, evidence_context=engine.evidence_context)
    except Exception:
        logger.warning("Could not read persisted M15 candle state; the current bar may be re-evaluated", exc_info=True)
        return {}
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring malformed persisted M15 candle state")
        return {}
    if not isinstance(stored, dict):
        return {}
    now_utc = datetime.now(timezone.utc)
    seeded: dict[str, datetime] = {}
    for symbol, value in stored.items():
        if symbol not in SYMBOLS:
            continue
        candle = _parse_timestamp_utc(value)
        if candle is None or candle > now_utc:
            continue
        seeded[symbol] = candle
    return seeded


def store_last_m15_candles(engine) -> None:
    payload = json.dumps(
        {symbol: candle.isoformat() for symbol, candle in sorted(engine._last_m15_candle_by_symbol.items())}
    )
    try:
        engine.repository.set_engine_runtime_state(LAST_M15_CANDLE_STATE_KEY, payload, evidence_context=engine.evidence_context)
    except Exception:
        logger.warning("Could not persist M15 candle state; a restart may re-evaluate this bar", exc_info=True)
