"""Research-only strategies can never run live or on the demo (owner decision 2026-11-04, design 3.5a).

Making ``d1_breakout_v1`` (H-009) live-eligible needs an approved SRS v1.x
revision and a visible edit to ``test_live_eligible_set_is_pinned``.
"""
from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path

import pandas as pd
import pytest

import main
from backtesting import run_replay
from core.strategy import (
    DEFAULT_STRATEGY_ID, StrategyNotLiveEligibleError, available_strategies, build_strategy, is_live_eligible,
    live_block_reason, registry, require_live_eligible,
)
from config_microcapital import get_policy_config
from database.repositories.memory import InMemoryEngineRepository
from tests.engine_harness import make_file_bridge_engine
from tests.replay_fixtures import FIXTURE_SYMBOLS, write_fixture_csvs
from tests.test_contract_engine import NOW_FIXED, _golden_bridge

POLICY = get_policy_config(mode_id="core_srs", env={})
EMPTY = lambda *a: pd.DataFrame()  # noqa: E731


@pytest.fixture()
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    return registry


def test_register_requires_live_eligible_and_a_reason_when_false(isolated_registry):
    factory = lambda **kw: None  # noqa: E731
    with pytest.raises(TypeError):
        isolated_registry.register_strategy("stub_no_flag", factory, None)  # type: ignore[call-arg]
    for reason in ("", "   "):
        with pytest.raises(ValueError, match="live_block_reason"):
            isolated_registry.register_strategy("stub_no_reason", factory, None, live_eligible=False,
                                                live_block_reason=reason)
    with pytest.raises(ValueError, match="must be a bool"):
        isolated_registry.register_strategy("stub_truthy", factory, None, live_eligible=1)


def test_live_eligible_set_is_pinned():
    assert {s for s in available_strategies() if is_live_eligible(s)} == {
        "trend_pullback_v1", "london_breakout_v1", "h1_channel_breakout_v1"}
    assert is_live_eligible("d1_breakout_v1") is False
    reason = live_block_reason("d1_breakout_v1")
    assert "SRS" in reason and "§5.2" in reason and "§5.3" in reason and "replay/research only" in reason
    assert live_block_reason("trend_pullback_v1") == ""


def test_build_refuses_ineligible_or_unknown_strategy_live_before_the_factory_runs(isolated_registry, monkeypatch):
    calls = []
    spec = isolated_registry._REGISTRY["d1_breakout_v1"]
    spy = lambda **kw: calls.append(kw) or spec.factory(**kw)  # noqa: E731
    monkeypatch.setitem(isolated_registry._REGISTRY, "d1_breakout_v1",
                        isolated_registry.StrategySpec("d1_breakout_v1", spy, spec.params_type, False,
                                                       spec.live_block_reason))
    kw = dict(symbol="GBPUSD", fetch_ohlc=EMPTY, fetch_spread=None, policy=POLICY)
    with pytest.raises(StrategyNotLiveEligibleError, match="not live-eligible") as exc:
        build_strategy("d1_breakout_v1", run_context="live", **kw)
    assert isinstance(exc.value, ValueError) and exc.value.reason == spec.live_block_reason
    assert calls == []
    assert build_strategy("d1_breakout_v1", run_context="research", **kw).strategy_id == "d1_breakout_v1"
    assert len(calls) == 1
    with pytest.raises(StrategyNotLiveEligibleError, match="unknown strategy id"):
        build_strategy("not_registered_v1", run_context="live", **kw)
    with pytest.raises(TypeError):
        build_strategy("d1_breakout_v1", **kw)  # run_context is required  # type: ignore[call-arg]
    require_live_eligible("trend_pullback_v1")


def test_live_strategy_is_the_default_with_no_cli_or_env_switch():
    assert main.LIVE_STRATEGY_ID == DEFAULT_STRATEGY_ID == "trend_pullback_v1"
    with pytest.raises(SystemExit):
        main.parse_args(["--mode", "smoke", "--strategy", "d1_breakout_v1"])
    source = inspect.getsource(main)
    assert "LIVE_STRATEGY_ID = DEFAULT_STRATEGY_ID" in source
    assert "os.getenv" not in source.split("LIVE_STRATEGY_ID = DEFAULT_STRATEGY_ID")[1].split("\n")[0]
    init = inspect.getsource(main.Engine.__init__)
    body = init.split("):\n", 1)[1]
    assert body.index("require_live_eligible(LIVE_STRATEGY_ID)") < body.index("self.bridge = bridge")


@pytest.mark.parametrize("strategy_id", ["d1_breakout_v1", "unknown_strategy_v1"])
def test_engine_refuses_to_start_with_a_research_only_or_unknown_strategy(tmp_path, monkeypatch, caplog,
                                                                            strategy_id):
    bridge_dir = _golden_bridge(tmp_path / "bridge")
    before = sorted(p.relative_to(bridge_dir).as_posix() for p in bridge_dir.rglob("*"))
    monkeypatch.setattr(main, "LIVE_STRATEGY_ID", strategy_id)
    repository = InMemoryEngineRepository()
    with caplog.at_level(logging.CRITICAL, logger="fx_ai_engine.main"):
        with pytest.raises(StrategyNotLiveEligibleError):
            make_file_bridge_engine(bridge_dir, monkeypatch, clock=lambda: NOW_FIXED, repository=repository)
    assert any(r.getMessage().startswith(f"STRATEGY_NOT_LIVE_ELIGIBLE strategy={strategy_id} reason=")
               for r in caplog.records)
    assert list((bridge_dir / "pending_signals").glob("*")) == []
    assert sorted(p.relative_to(bridge_dir).as_posix() for p in bridge_dir.rglob("*")) == before
    tables = [v for v in vars(repository).values() if isinstance(v, (list, dict))]
    assert all(len(t) == 0 for t in tables)


def test_replay_report_of_a_research_only_strategy_says_so(tmp_path, monkeypatch):
    data_dir = write_fixture_csvs(tmp_path / "data")
    registry_path = tmp_path / "registry.jsonl"
    monkeypatch.setattr(run_replay, "REGISTRY", registry_path)
    out = tmp_path / "out"
    argv = ["--data-dir", str(data_dir), "--symbols", *FIXTURE_SYMBOLS, "--start", "2026-10-30T00:00:00Z",
            "--holdout-months", "0", "--out", str(out), "--workers", "1", "--no-cache", "--no-gate",
            "--strategy", "d1_breakout_v1"]
    assert run_replay.main(argv) == 0
    stem = "d1_breakout_v1__baseline__policy__srs__base"
    report = json.loads((out / f"{stem}.json").read_text(encoding="utf-8"))
    assert report["live_eligible"] is False
    assert report["live_block_reason"] == live_block_reason("d1_breakout_v1")
    assert report["strategy_params"]["channel_d1_bars"] == 20
    lines = (out / f"{stem}.md").read_text(encoding="utf-8").splitlines()
    assert lines[1] == f"Research only: {live_block_reason('d1_breakout_v1')}"
    (row,) = [json.loads(line) for line in Path(registry_path).read_text().splitlines()]
    assert row["live_eligible"] is False and row["live_block_reason"] == report["live_block_reason"]
