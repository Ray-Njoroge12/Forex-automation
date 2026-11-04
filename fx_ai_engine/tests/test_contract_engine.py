"""Contract tests for ``main.Engine`` written before the Phase 4 split (stage C).

They pin what the split must not change: the Engine method names that tests and
tools call, the order of one run-loop iteration, and the funnel and risk-event
record of one decision cycle on a fixed file bridge. The golden record was
produced by the unsplit ``main.py``; regenerate it only for an intended
behaviour change (``UPDATE_CONTRACT_GOLDEN=1``), never to make a refactor pass.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.account_status import AccountStatus
from main import Engine
from tests.engine_harness import make_file_bridge_engine
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)
from tests.test_file_bridge_connection import build_bridge

GOLDEN = Path(__file__).parent / "fixtures" / "contract" / "decision_cycle_golden.json"
LIVE_BARS = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
TF_SECONDS = {"M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
# 2026-11-03 16:30:05 UTC: the live engine routed GBPUSD BUY on this bar (London/NY overlap).
NOW_FIXED = datetime(2026, 11, 3, 16, 30, 5, tzinfo=timezone.utc)
# Modules that may own the engine's wall-clock reads (main today; application/* after the split).
CLOCK_OWNER_MODULES = (
    "main",
    "application.decision_cycle",
    "application.market_data_service",
    "application.feedback_service",
    "application.engine_runtime",
)

ENGINE_METHODS = (
    "__init__",
    "_insert_trade_proposal",
    "_insert_risk_event",
    "_insert_funnel_event",
    "_insert_ml_shadow_event",
    "_virtual_balance",
    "_pre_route_feasibility",
    "_strategic_risk_eligibility",
    "_fail_closed_bridge_uncertainty",
    "_fail_closed_preserve_10_bridge",
    "_enforce_execution_feedback_integrity",
    "_validate_bridge_path",
    "_update_account_state",
    "_ensure_consecutive_losses_seeded",
    "_record_consecutive_loss_update",
    "_record_broker_close_time",
    "_is_snapshot_stale_after_broker_close",
    "_fetch_broker_closure_summary",
    "_recover_missing_trade_closures_from_broker",
    "_consume_feedback",
    "_is_new_m15_candle",
    "_load_last_m15_candles",
    "_store_last_m15_candles",
    "_evaluate_symbol",
    "_decision_cycle",
    "run",
)

MAIN_NAMES = (
    "Engine",
    "SYMBOLS",
    "parse_args",
    "main",
    "evaluate_runtime_policy_approval",
    "evaluate_preserve_10_startup_approval",
    "_demo_account_verified",
    "_snapshot_age_seconds",
    "_parse_timestamp_utc",
    "_execution_feedback_dedupe_key",
    "_exit_feedback_dedupe_key",
    "_normalise_legacy_exit_close_time",
    "_estimate_recovered_r_multiple",
)


def test_engine_method_surface_is_kept():
    missing = [name for name in ENGINE_METHODS if not callable(getattr(Engine, name, None))]
    assert missing == []


def test_main_module_names_are_kept():
    main = importlib.import_module("main")
    assert [name for name in MAIN_NAMES if not hasattr(main, name)] == []


def test_run_loop_order_is_unchanged(monkeypatch):
    calls: list[str] = []
    engine = Engine.__new__(Engine)
    engine.bridge = SimpleNamespace(is_demo_account=lambda: True)
    engine.use_mock = True
    engine.account_status = AccountStatus()
    engine.mock_feedback = None
    engine.evidence_context = SimpleNamespace(evidence_stream="x", account_scope="x")
    engine._stale_episode_active = False
    engine._consecutive_losses_dirty = False
    engine.metrics = SimpleNamespace(inc=lambda *_: None)
    # Persistence seam since the services take an injected repository: the
    # end-of-loop metrics write is a no-op, as the patch below was before.
    engine.repository = SimpleNamespace(insert_account_metrics=lambda *_a, **_k: None)
    for name in (
        "_consume_feedback",
        "_recover_missing_trade_closures_from_broker",
        "_update_account_state",
        "_enforce_execution_feedback_integrity",
        "_decision_cycle",
    ):
        setattr(engine, name, lambda name=name: calls.append(name))

    def new_candle() -> bool:
        calls.append("_is_new_m15_candle")
        return True

    engine._is_new_m15_candle = new_candle
    for module in _clock_modules():
        if hasattr(module, "update_health_status"):
            monkeypatch.setattr(module, "update_health_status", lambda **_: None)
        if hasattr(module, "insert_account_metrics"):
            monkeypatch.setattr(module, "insert_account_metrics", lambda *_a, **_k: None)
    engine.run(mode="smoke")
    assert calls == [
        "_consume_feedback",
        "_recover_missing_trade_closures_from_broker",
        "_update_account_state",
        "_enforce_execution_feedback_integrity",
        "_is_new_m15_candle",
        "_decision_cycle",
    ]


def _clock_modules() -> list:
    modules = []
    for name in CLOCK_OWNER_MODULES:
        if importlib.util.find_spec(name) is None:
            continue
        modules.append(importlib.import_module(name))
    return modules


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D401 - datetime API
        return NOW_FIXED if tz is None else NOW_FIXED.astimezone(tz)


def _freeze_engine_clock(monkeypatch) -> None:
    for module in _clock_modules():
        if getattr(module, "datetime", None) is datetime:
            monkeypatch.setattr(module, "datetime", _FrozenDatetime)


def _load_live_bars(symbol: str, tf: str) -> dict:
    payload = json.loads((LIVE_BARS / f"bars_{symbol}_{tf}.json").read_text())
    cutoff = int(NOW_FIXED.timestamp())
    payload["bars"] = [row for row in payload["bars"] if row[0] + TF_SECONDS[tf] <= cutoff]
    payload["generated_at"] = (NOW_FIXED - timedelta(minutes=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return payload


def _golden_bridge(root: Path) -> Path:
    bridge_dir = build_bridge(root, NOW_FIXED, open_tickets=())
    market = bridge_dir / "market"
    status = json.loads((market / "status.json").read_text())
    for symbol in ("GBPUSD", "AUDUSD"):
        for tf in TF_SECONDS:
            payload = _load_live_bars(symbol, tf)
            (market / f"bars_{symbol}_{tf}.json").write_text(json.dumps(payload))
            status["latest_closed_bars"][symbol][tf] = payload["bars"][-1][0]
    (market / "status.json").write_text(json.dumps(status))
    quotes_path = bridge_dir / "feedback" / "market_quotes.json"
    quotes = json.loads(quotes_path.read_text())
    # Live spreads at 16:30: GBPUSD 0.0 pips, AUDUSD 0.1 pips.
    quotes["quotes"] += [
        {"symbol": "GBPUSD", "bid": 1.34175, "ask": 1.34175, "tick_age_seconds": 1},
        {"symbol": "AUDUSD", "bid": 0.65500, "ask": 0.65501, "tick_age_seconds": 1},
    ]
    quotes_path.write_text(json.dumps(quotes))
    return bridge_dir


def _synthetic_calendar() -> list[dict]:
    # One far-away JPY event: no blackout for any symbol at NOW_FIXED, coverage beyond 7 days.
    return [{
        "datetime_utc": (NOW_FIXED + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "currency": "JPY", "impact": "high", "event": "contract-test event", "source_url": "",
        "blackout_before_minutes": 30, "blackout_after_minutes": 15,
    }]


def _decision_record(connection) -> dict:
    with connection() as conn:
        funnel = [
            [row["stage"], row["outcome"], row["reason_code"], row["symbol"]]
            for row in conn.execute(
                "SELECT stage, outcome, reason_code, symbol FROM decision_funnel_events ORDER BY id"
            )
        ]
        risk = [row["rule_name"] for row in conn.execute("SELECT rule_name FROM risk_events ORDER BY id")]
    return {"funnel": funnel, "risk_events": risk}


def test_decision_cycle_matches_golden_record(tmp_path, monkeypatch, isolated_db):
    bridge_dir = _golden_bridge(tmp_path / "bridge")
    _freeze_engine_clock(monkeypatch)
    for module in _clock_modules():
        for name in ("alert_risk_halt", "alert_trade_execution", "alert_trade_exit"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, lambda *a, **k: None)
    engine = make_file_bridge_engine(bridge_dir, monkeypatch, clock=lambda: NOW_FIXED)
    # Pin the inputs that depend on local files rather than the bridge.
    engine._calendar_events = _synthetic_calendar()
    engine.ranker_loaded = False

    engine._update_account_state()
    engine._decision_cycle()
    record = _decision_record(isolated_db)

    if os.getenv("UPDATE_CONTRACT_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    assert record == json.loads(GOLDEN.read_text(encoding="utf-8"))
    routed = [row for row in record["funnel"] if row[0] == "ROUTER"]
    assert routed == [["ROUTER", "ROUTED", "ROUTED_TO_MT5", "GBPUSD"]]
    assert len(list((bridge_dir / "pending_signals").glob("*.json"))) == 1
