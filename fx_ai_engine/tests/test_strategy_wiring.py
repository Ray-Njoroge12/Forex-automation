"""Live engine and replay use the same strategy object (stage D, ADR 0014)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backtesting import replay_cache, run_replay
from backtesting.unified_replay import (
    Candidate, CostModel, _candidate_cache_parts, generate_candidates, load_bridge_bar_exports,
)
from config_microcapital import get_policy_config
from core.strategy import DEFAULT_STRATEGY_ID, STRATEGY_ERROR, TECH_SKIPPED_REGIME_REJECTED
from core.strategy.trend_pullback import TrendPullbackStrategy
from research import bias_audit
from tests.engine_harness import make_file_bridge_engine
from tests.replay_fixtures import FIXTURE_SYMBOLS, write_fixture_csvs
from tests.strategy_contract import session_decision_times
from tests.strategy_stubs import _always_signal, _raise, _regime_reject, _register_stub, isolated_registry  # noqa: F401
from tests.test_contract_engine import (
    NOW_FIXED, _clock_modules, _freeze_engine_clock, _golden_bridge, _synthetic_calendar,
)
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)

FIXTURE = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
POLICY = get_policy_config(mode_id="core_srs", env={})


@pytest.fixture(scope="module")
def frames():
    return load_bridge_bar_exports(FIXTURE, list(FIXTURE_SYMBOLS))


# ----------------------------------------------------------------------
# Replay wiring
# ----------------------------------------------------------------------
def test_stub_strategy_flows_through_generate_candidates_with_its_own_cache_key(frames, isolated_registry):
    _register_stub(isolated_registry, "stub_always", _always_signal)
    times = session_decision_times(frames, "GBPUSD", last=6)
    cost = CostModel()
    cands, funnel = generate_candidates("GBPUSD", frames, POLICY, cost, decision_times=times,
                                        strategy_id="stub_always")
    assert len(cands) == len(times) and funnel["CANDIDATE"] == len(times)
    assert {c.strategy_id for c in cands} == {"stub_always"}
    assert cands[0].signal["reason_code"] == "STUB_BUY"
    default, _ = generate_candidates("GBPUSD", frames, POLICY, cost, decision_times=times)
    assert {c.strategy_id for c in default} <= {DEFAULT_STRATEGY_ID}

    _register_stub(isolated_registry, "stub_reject", _regime_reject)
    none, funnel = generate_candidates("GBPUSD", frames, POLICY, cost, decision_times=times,
                                       strategy_id="stub_reject")
    assert none == [] and funnel == {"REGIME:STUB_REGIME_REJECT": len(times)}

    data = Path("/nonexistent")
    parts = {sid: _candidate_cache_parts("GBPUSD", data, POLICY, cost, None, None, sid)
             for sid in (DEFAULT_STRATEGY_ID, "stub_always")}
    keys = {sid: replay_cache.cache_key("candidates", **p) for sid, p in parts.items()}
    assert keys[DEFAULT_STRATEGY_ID] != keys["stub_always"]
    lbo = [_candidate_cache_parts("GBPUSD", data, POLICY, cost, None, None, "london_breakout_v1", p)
           for p in (None, {"min_stop_pips": 25})]
    assert lbo[0]["strategy_params"]["min_stop_pips"] == 20.0
    assert replay_cache.cache_key("candidates", **lbo[0]) != replay_cache.cache_key("candidates", **lbo[1])
    assert "core/strategy/**/*.py" in replay_cache.CANDIDATE_CODE_PATHS


def test_candidate_keeps_positional_constructor_and_payload_carries_strategy_id():
    from backtesting.unified_replay import _candidates_from_payload, _candidates_to_payload

    t = pd.Timestamp("2026-11-03T16:30:00Z")
    old = Candidate(t, "GBPUSD", {"a": 1}, {"b": 2})
    assert old.strategy_id == DEFAULT_STRATEGY_ID
    new = Candidate(t, "GBPUSD", {"a": 1}, {"b": 2}, "london_breakout_v1")
    (back,), _ = _candidates_from_payload(json.loads(json.dumps(_candidates_to_payload([new], {}))))
    assert back == new


def test_r1_bias_audit_runs_the_selected_strategy(frames, isolated_registry):
    _register_stub(isolated_registry, "stub_always", _always_signal)
    gbp = {"GBPUSD": frames["GBPUSD"]}
    report = bias_audit.run_bias_audit(gbp, POLICY, max_decisions=4, long_bars=300, eval_points=5,
                                       strategy_id="stub_always")
    assert report["strategy_id"] == "stub_always"
    assert report["truncation"]["status"] == "PASS" and report["truncation"]["checked"] == 2  # every time is a stub candidate; max_decisions // 2 are sampled
    assert report["future_corruption"]["status"] == "PASS"
    # The stub fetches only 50 M15 bars; the default strategy would show H1/H4/M15 at 350.
    assert report["warmup_parity"]["status"] == "PASS"
    assert report["warmup_parity"]["live_fetch_bars"] == {"M15": 50}
    sampled = report["decision_times_checked"]["GBPUSD"]
    cands, _ = generate_candidates("GBPUSD", gbp, POLICY, CostModel(), decision_times=[pd.Timestamp(sampled[0])],
                                   strategy_id="stub_always")
    assert cands and cands[0].strategy_id == "stub_always"


@pytest.fixture()
def replay_env(tmp_path, monkeypatch):
    data_dir = write_fixture_csvs(tmp_path / "data")
    registry_path = tmp_path / "registry.jsonl"
    monkeypatch.setattr(run_replay, "REGISTRY", registry_path)
    start = "2026-10-30T00:00:00Z"
    return {"data": data_dir, "start": start, "cache": tmp_path / "cache", "registry": registry_path, "tmp": tmp_path}


def _argv(env, out: Path, *extra: str) -> list[str]:
    return ["--data-dir", str(env["data"]), "--symbols", *FIXTURE_SYMBOLS, "--start", env["start"],
            "--holdout-months", "0", "--out", str(out), "--workers", "1", "--cache-dir", str(env["cache"]),
            "--no-gate", *extra]


def test_run_replay_with_a_stub_strategy_writes_strategy_fields(replay_env, isolated_registry):
    _register_stub(isolated_registry, "stub_always", _always_signal)
    out = replay_env["tmp"] / "out"
    assert run_replay.main(_argv(replay_env, out, "--strategy", "stub_always")) == 0
    stem = "stub_always__baseline__policy__srs__base"
    report = json.loads((out / f"{stem}.json").read_text(encoding="utf-8"))
    assert (report["strategy_id"], report["strategy_params"], report["live_eligible"]) == ("stub_always", {}, True)
    assert "live_block_reason" not in report
    assert report["development"]["trades"]["trades"] > 0
    md = (out / f"{stem}.md").read_text(encoding="utf-8").splitlines()
    assert md[0] == run_replay.NO_GATE_LINE and md[1] == ""
    (row,) = [json.loads(line) for line in replay_env["registry"].read_text().splitlines()]
    assert (row["strategy_id"], row["strategy_params"], row["variant"]) == ("stub_always", {}, stem[:-6])
    trades = pd.read_csv(out / f"{stem}_trades.csv")
    assert set(trades["strategy_id"]) == {"stub_always"}


def test_run_replay_default_label_is_unchanged(replay_env):
    out = replay_env["tmp"] / "out"
    assert run_replay.main(_argv(replay_env, out)) == 0
    report = json.loads((out / "baseline__policy__srs__base.json").read_text(encoding="utf-8"))
    assert (report["strategy_id"], report["strategy_params"]) == (DEFAULT_STRATEGY_ID, {})


@pytest.mark.parametrize("extra", [
    ["--strategy", "london_breakout_v1", "--variants", "baseline", "pullback_2"],
    ["--strategy", "london_breakout_v1", "--strategy-params", "[1, 2]"],
    ["--strategy", "london_breakout_v1", "--strategy-params", "{not json"],
    ["--strategy", "london_breakout_v1", "--strategy-params", '{"bogus": 1}'],
    ["--strategy", "trend_pullback_v1", "--strategy-params", '{"x": 1}'],
    ["--strategy", "unknown_v1"],
])
def test_run_replay_rejects_bad_strategy_arguments(replay_env, extra, capsys):
    with pytest.raises(SystemExit) as exc:
        run_replay.main(_argv(replay_env, replay_env["tmp"] / "out", *extra))
    assert exc.value.code == 2
    assert not (replay_env["tmp"] / "out").exists() or not list((replay_env["tmp"] / "out").glob("*.json"))


# ----------------------------------------------------------------------
# Live wiring (file-bridge engine harness, temp bridge only)
# ----------------------------------------------------------------------
def _live_engine(tmp_path, monkeypatch, strategy_id: str):
    import main

    monkeypatch.setattr(main, "LIVE_STRATEGY_ID", strategy_id)
    bridge_dir = _golden_bridge(tmp_path / "bridge")
    _freeze_engine_clock(monkeypatch)
    for module in _clock_modules():
        for name in ("alert_risk_halt", "alert_trade_execution", "alert_trade_exit"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, lambda *a, **k: None)
    engine = make_file_bridge_engine(bridge_dir, monkeypatch, clock=lambda: NOW_FIXED)
    engine._calendar_events = _synthetic_calendar()
    engine.ranker_loaded = False
    engine._update_account_state()
    return engine, bridge_dir


def _funnel(connection) -> list[list[str]]:
    with connection() as conn:
        return [[r["stage"], r["outcome"], r["reason_code"], r["symbol"]]
                for r in conn.execute("SELECT stage, outcome, reason_code, symbol FROM decision_funnel_events ORDER BY id")]


def _risk_rules(connection) -> list[str]:
    with connection() as conn:
        return [r["rule_name"] for r in conn.execute("SELECT rule_name FROM risk_events ORDER BY id")]


def test_live_engine_maps_a_stub_regime_reject_to_regime_reject_and_technical_skip(
        tmp_path, monkeypatch, isolated_db, isolated_registry):
    _register_stub(isolated_registry, "stub_reject", _regime_reject)
    engine, bridge_dir = _live_engine(tmp_path, monkeypatch, "stub_reject")
    assert {s.strategy_id for s in engine.strategies.values()} == {"stub_reject"}
    assert set(engine.agents["GBPUSD"]) == {"adversarial"}
    engine._decision_cycle()
    rows = [r for r in _funnel(isolated_db) if r[0] != "SESSION"]
    symbols = list(engine.strategies)
    assert rows == [row for s in symbols for row in (["REGIME", "REJECT", "STUB_REGIME_REJECT", s],
                                                     ["TECHNICAL", "SKIP", TECH_SKIPPED_REGIME_REJECTED, s])]
    assert list((bridge_dir / "pending_signals").glob("*.json")) == []


def test_live_engine_fails_closed_per_symbol_when_the_strategy_raises(
        tmp_path, monkeypatch, isolated_db, isolated_registry):
    _register_stub(isolated_registry, "stub_error", _raise)
    engine, bridge_dir = _live_engine(tmp_path, monkeypatch, "stub_error")
    engine._decision_cycle()  # does not raise
    rows = [r for r in _funnel(isolated_db) if r[0] in {"REGIME", "TECHNICAL"}]
    assert rows == [row for s in engine.strategies for row in (["REGIME", "REJECT", STRATEGY_ERROR, s],
                                                               ["TECHNICAL", "SKIP", TECH_SKIPPED_REGIME_REJECTED, s])]
    assert _risk_rules(isolated_db) == [STRATEGY_ERROR] * len(engine.strategies)
    assert list((bridge_dir / "pending_signals").glob("*.json")) == []


def test_live_engine_hard_risk_still_blocks_a_stub_strategy_signal(
        tmp_path, monkeypatch, isolated_db, isolated_registry):
    def factory(*, symbol, fetch_ohlc, fetch_spread, policy, params, regime_cache_key):
        inner = TrendPullbackStrategy(symbol, fetch_ohlc, fetch_spread, policy)
        inner.strategy_id = "stub_delegate"
        return inner

    isolated_registry.register_strategy("stub_delegate", factory, None, live_eligible=True)
    engine, bridge_dir = _live_engine(tmp_path, monkeypatch, "stub_delegate")
    engine.account_status.is_trading_halted = True
    engine._decision_cycle()
    gbp = [r[:3] for r in _funnel(isolated_db) if r[3] == "GBPUSD"]
    assert ["TECHNICAL", "PASS", "TECH_CONFIRMED_BUY"] in gbp
    assert ["HARD_RISK", "REJECT", "RISK_HALTED"] in gbp
    assert not any(r[0] == "ROUTER" for r in _funnel(isolated_db))
    assert list((bridge_dir / "pending_signals").glob("*.json")) == []
