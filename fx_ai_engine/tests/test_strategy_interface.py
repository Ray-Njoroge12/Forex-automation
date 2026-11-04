"""Strategy plug-in interface (stage D, ADR 0004): registry, guard, static rules, default strategy contract."""
from __future__ import annotations

import ast
import math
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

import core.strategy as strategy_pkg
from backtesting.unified_replay import PointInTimeFeed, load_bridge_bar_exports
from config_microcapital import get_policy_config
from core.strategy import (
    DEFAULT_STRATEGY_ID, STRATEGY_ERROR, TECH_SKIPPED_REGIME_REJECTED, build_strategy, canonical_params,
)
from core.strategy.guard import GuardedStrategy, check_signal
from core.strategy.trend_pullback import TrendPullbackStrategy
from tests.replay_fixtures import FIXTURE_SYMBOLS
from tests.strategy_contract import assert_strategy_contract, fingerprint, session_decision_times
from tests.strategy_stubs import StubStrategy, _always_signal, _raise, _signal, isolated_registry  # noqa: F401

FIXTURE = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
POLICY = get_policy_config(mode_id="core_srs", env={})
LIVE_DECISION = pd.Timestamp("2026-11-03T16:30:00Z")
STRATEGY_DIR = Path(__file__).resolve().parents[1] / "core" / "strategy"


@pytest.fixture(scope="module")
def frames():
    return load_bridge_bar_exports(FIXTURE, list(FIXTURE_SYMBOLS))


def _build(strategy_id: str, frames, t, **kw):
    feed = PointInTimeFeed(frames)
    feed.set_time(t)
    return build_strategy(strategy_id, symbol="GBPUSD", fetch_ohlc=feed.fetch_ohlc,
                          fetch_spread=lambda s: 0.00008, policy=POLICY, run_context="research", **kw)


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
def test_registry_lists_the_default_and_the_approved_hypotheses():
    assert strategy_pkg.available_strategies() == (
        "d1_breakout_v1", "h1_channel_breakout_v1", "london_breakout_v1", "trend_pullback_v1")
    assert DEFAULT_STRATEGY_ID == "trend_pullback_v1"


def test_unknown_strategy_and_bad_arguments_raise_value_error(frames):
    with pytest.raises(ValueError, match="unknown strategy 'nope_v1'"):
        _build("nope_v1", frames, LIVE_DECISION)
    with pytest.raises(ValueError, match="run_context"):
        build_strategy(DEFAULT_STRATEGY_ID, symbol="GBPUSD", fetch_ohlc=lambda *a: pd.DataFrame(),
                       fetch_spread=None, policy=POLICY, run_context="demo")
    with pytest.raises(ValueError, match="on_error"):
        _build(DEFAULT_STRATEGY_ID, frames, LIVE_DECISION, on_error="ignore")
    policy = {k: v for k, v in POLICY.items() if k != "MIN_RISK_REWARD"}
    with pytest.raises(ValueError, match="MIN_RISK_REWARD"):
        build_strategy(DEFAULT_STRATEGY_ID, symbol="GBPUSD", fetch_ohlc=lambda *a: pd.DataFrame(),
                       fetch_spread=None, policy=policy, run_context="research")


def test_register_rejects_duplicates_bad_ids_and_non_frozen_params(isolated_registry):
    factory = lambda **kw: None  # noqa: E731
    with pytest.raises(ValueError, match="already registered"):
        isolated_registry.register_strategy(DEFAULT_STRATEGY_ID, factory, None, live_eligible=True)
    for bad in ("X_strategy", "ab", "has-dash", "a" * 42, 7):
        with pytest.raises(ValueError, match="does not match"):
            isolated_registry.register_strategy(bad, factory, None, live_eligible=True)

    class NotFrozen:
        pass

    with pytest.raises(ValueError, match="frozen dataclass"):
        isolated_registry.register_strategy("stub_params", factory, NotFrozen, live_eligible=True)


@pytest.mark.parametrize("params, match", [
    ({"bogus": 1}, "no param"),
    ({"min_stop_pips": True}, "must be a number"),
    ({"min_stop_pips": "20"}, "must be a number"),
    ({"min_stop_pips": math.nan}, "must be finite"),
    ({"max_stop_pips": math.inf}, "must be finite"),
    ({"min_stop_pips": 4.0}, "outside"),
    ({"range_end_hour": 7.5}, "must be an integer"),
    ({"min_stop_pips": 60.0, "max_stop_pips": 50.0}, "min_stop_pips < max_stop_pips"),
])
def test_params_are_validated_by_key(params, match):
    with pytest.raises(ValueError, match=match):
        canonical_params("london_breakout_v1", params)


def test_canonical_params_fill_defaults_sorted_and_default_strategy_takes_none():
    params = canonical_params("london_breakout_v1", {"range_end_hour": 7.0, "min_stop_pips": 25})
    assert list(params) == sorted(params)
    assert params["range_end_hour"] == 7 and isinstance(params["range_end_hour"], int)
    assert params["min_stop_pips"] == 25.0 and params["max_stop_pips"] == 60.0
    assert canonical_params(DEFAULT_STRATEGY_ID) == {}
    with pytest.raises(ValueError, match="takes no params"):
        canonical_params(DEFAULT_STRATEGY_ID, {"x": 1})


# ----------------------------------------------------------------------
# Guard
# ----------------------------------------------------------------------
@pytest.mark.parametrize("overrides, reason, field", [
    ({"symbol": "EURUSD"}, "STRATEGY_SIGNAL_INVALID", "symbol"),
    ({"direction": "LONG"}, "STRATEGY_SIGNAL_INVALID", "direction"),
    ({"stop_pips": 0.0}, "STRATEGY_SIGNAL_INVALID", "stop_pips"),
    ({"stop_pips": math.nan}, "STRATEGY_SIGNAL_INVALID", "stop_pips"),
    ({"take_profit_pips": -1.0}, "STRATEGY_SIGNAL_INVALID", "take_profit_pips"),
    ({"risk_reward": 2.19}, "STRATEGY_SIGNAL_INVALID", "risk_reward"),
    ({"risk_reward": math.inf}, "STRATEGY_SIGNAL_INVALID", "risk_reward"),
    ({"take_profit_pips": 54.97}, "STRATEGY_SIGNAL_INVALID", "take_profit_pips"),
    ({"order_type": "LIMIT"}, "STRATEGY_ORDER_TYPE_UNSUPPORTED", "order_type"),
    ({"tp_mode": "RUNNER"}, "STRATEGY_SIGNAL_INVALID", "tp_mode"),
    ({"be_trigger_r": -0.1}, "STRATEGY_SIGNAL_INVALID", "be_trigger_r"),
    ({"partial_close_r": math.nan}, "STRATEGY_SIGNAL_INVALID", "partial_close_r"),
    ({"trailing_atr_mult": -1.0}, "STRATEGY_SIGNAL_INVALID", "trailing_atr_mult"),
])
def test_guard_rejects_each_rule(overrides, reason, field):
    assert check_signal(_signal(), symbol="GBPUSD", min_rr=2.2) is None
    got = check_signal(_signal(**overrides), symbol="GBPUSD", min_rr=2.2)
    assert got is not None and got[0] == reason
    assert got[1].startswith(f"field={field} value=") and " rule=" in got[1]


def test_guard_turns_a_bad_signal_into_a_technical_reject():
    bad = lambda s: replace(_always_signal(s), signal=_signal(symbol=s.symbol, risk_reward=1.0))  # noqa: E731
    guarded = GuardedStrategy(StubStrategy("stub_guard", "GBPUSD", lambda *a: pd.DataFrame(), bad), min_rr=2.2)
    decision = guarded.evaluate()
    assert decision.signal is None and decision.regime_passed
    assert decision.reason_code == "STRATEGY_SIGNAL_INVALID"
    assert decision.details == "field=risk_reward value=1.0 rule=finite>=2.2"


def test_guard_exception_paths_raise_or_fail_closed(caplog):
    inner = StubStrategy("stub_error", "GBPUSD", lambda *a: pd.DataFrame(), _raise)
    with pytest.raises(RuntimeError):
        GuardedStrategy(inner, min_rr=2.2, on_error="raise").evaluate()
    with caplog.at_level("ERROR", logger="fx_ai_engine.strategy"):
        decision = GuardedStrategy(inner, min_rr=2.2, on_error="reject").evaluate()
    assert not decision.regime_passed and decision.signal is None
    assert decision.regime_reason_code == STRATEGY_ERROR and decision.regime.regime == "NO_TRADE"
    assert decision.regime_details.startswith("RuntimeError: stub strategy failure")
    assert len(decision.regime_details) == 200
    assert decision.reason_code == TECH_SKIPPED_REGIME_REJECTED
    assert any(r.exc_info for r in caplog.records)
    # A strategy that returns the wrong type takes the same path.
    wrong = StubStrategy("stub_wrong", "GBPUSD", lambda *a: pd.DataFrame(), lambda s: {"signal": None})
    assert GuardedStrategy(wrong, min_rr=2.2, on_error="reject").evaluate().regime_details.startswith("TypeError")


# ----------------------------------------------------------------------
# Static rules for core/strategy (completed bars, no clock, no bridge/DB)
# ----------------------------------------------------------------------
FORBIDDEN_IMPORTS = ("MetaTrader5", "core.mt5_bridge", "core.file_bridge_connection", "bridge", "database")
CLOCK_CALLS = {("datetime", "now"), ("datetime", "utcnow"), ("time", "time"), ("datetime", "today")}
CLOCK_HELPERS = {("base.py", "new_trade_id"), ("base.py", "utc_now_iso")}


def test_strategy_modules_import_no_bridge_or_database_and_read_no_clock():
    files = sorted(STRATEGY_DIR.glob("*.py"))
    assert {f.name for f in files} >= {"__init__.py", "base.py", "guard.py", "registry.py", "common.py",
                                       "trend_pullback.py", "london_breakout.py", "h1_channel_breakout.py",
                                       "d1_breakout.py"}
    problems = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_IMPORTS):
                    problems.append(f"{path.name}: imports {name}")
        owners: dict[ast.AST, str] = {}

        def visit(node: ast.AST, owner: str) -> None:
            for child in ast.iter_child_nodes(node):
                name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
                owners[child] = name
                visit(child, name)

        visit(tree, "<module>")
        for node, owner in owners.items():
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and (node.func.value.id, node.func.attr) in CLOCK_CALLS
                    and (path.name, owner) not in CLOCK_HELPERS):
                problems.append(f"{path.name}:{owner}: calls {node.func.value.id}.{node.func.attr}")
    assert problems == []


# ----------------------------------------------------------------------
# Default strategy on the fixture
# ----------------------------------------------------------------------
def _factory(strategy_id: str, symbol: str, **kw):
    return lambda fetch, spread: build_strategy(strategy_id, symbol=symbol, fetch_ohlc=fetch, fetch_spread=spread,
                                                policy=POLICY, run_context="research", **kw)


@pytest.mark.parametrize("symbol", FIXTURE_SYMBOLS)
def test_default_strategy_meets_the_contract_on_the_fixture(frames, symbol):
    times = sorted(set(session_decision_times(frames, symbol, last=16, step=2)) | {LIVE_DECISION})
    decisions = assert_strategy_contract(_factory(DEFAULT_STRATEGY_ID, symbol), frames, times, policy=POLICY)
    live = decisions[times.index(LIVE_DECISION)]
    expected = {"GBPUSD": ("BUY", 13.60), "AUDUSD": ("SELL", 7.93)}[symbol]
    assert live.signal is not None and (live.signal.direction, live.signal.stop_pips) == expected
    assert all(d.strategy_id == DEFAULT_STRATEGY_ID for d in decisions)


def test_default_strategy_regime_cache_on_equals_off(frames):
    times = session_decision_times(frames, "GBPUSD", last=24)
    feed_on, feed_off = PointInTimeFeed(frames), PointInTimeFeed(frames)
    on = build_strategy(DEFAULT_STRATEGY_ID, symbol="GBPUSD", fetch_ohlc=feed_on.fetch_ohlc, fetch_spread=None,
                        policy=POLICY, run_context="research",
                        regime_cache_key=lambda: feed_on.closed_count("GBPUSD", "H1"))
    off = build_strategy(DEFAULT_STRATEGY_ID, symbol="GBPUSD", fetch_ohlc=feed_off.fetch_ohlc, fetch_spread=None,
                         policy=POLICY, run_context="research")
    calls = []
    original = on.inner.regime_agent.evaluate
    on.inner.regime_agent.evaluate = lambda tf: calls.append(tf) or original(tf)
    for t in times:
        feed_on.set_time(t)
        feed_off.set_time(t)
        assert fingerprint(on.evaluate()) == fingerprint(off.evaluate()), t
    assert 0 < len(calls) < len(times)  # the cache was used


def test_default_strategy_reuses_core_agents_unchanged():
    strategy = build_strategy(DEFAULT_STRATEGY_ID, symbol="GBPUSD", fetch_ohlc=lambda *a: pd.DataFrame(),
                              fetch_spread=None, policy=POLICY, run_context="live")
    assert isinstance(strategy, GuardedStrategy) and isinstance(strategy.inner, TrendPullbackStrategy)
    decision = strategy.evaluate()
    assert (decision.regime_passed, decision.regime_reason_code) == (False, "REGIME_DATA_UNAVAILABLE")
    assert decision.details == "regime=NO_TRADE regime_reason=REGIME_DATA_UNAVAILABLE trend_state=UNKNOWN"
