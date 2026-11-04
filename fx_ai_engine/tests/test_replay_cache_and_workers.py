"""Replay resource use: candidate cache, worker cap and clean child shutdown (STATUS issue 3)."""
from __future__ import annotations

import gzip
import multiprocessing
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from backtesting import replay_cache, unified_replay
from backtesting.unified_replay import CostModel, generate_all_candidates
from config_microcapital import apply_agent_threshold_overrides, get_policy_config
from tests.replay_fixtures import FIXTURE_SYMBOLS, write_fixture_csvs

POLICY = get_policy_config(mode_id="core_srs", env={})
COST = CostModel()
START = pd.Timestamp("2026-11-01T00:00:00Z")
END = pd.Timestamp("2026-11-03T18:00:00Z")


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory) -> Path:
    return write_fixture_csvs(tmp_path_factory.mktemp("replay_data"))


def _comparable(candidates):
    """Agents stamp trade_id and timestamp_utc with wall-clock time; compare everything else."""
    volatile = {"trade_id", "timestamp_utc"}
    return [
        (c.decision_time, c.symbol,
         {k: v for k, v in c.signal.items() if k not in volatile},
         {k: v for k, v in c.regime.items() if k not in volatile})
        for c in candidates
    ]


def _run(data_dir, cache_dir, *, policy=POLICY, cost=COST, start=START, end=END, workers=1):
    return generate_all_candidates(data_dir, policy, cost, symbols=FIXTURE_SYMBOLS, start=start, end=end,
                                   workers=workers, cache_dir=cache_dir)


def _forbid_generation(monkeypatch):
    def _fail(*_args, **_kwargs):
        raise AssertionError("candidate generation ran despite a cache hit")
    monkeypatch.setattr(unified_replay, "generate_candidates", _fail)


def test_cache_miss_then_hit_returns_equal_candidates_and_funnels(data_dir, tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    first, first_funnels = _run(data_dir, cache_dir)
    assert sum(sum(f.values()) for f in first_funnels.values()) > 0
    assert len(list((cache_dir / "candidates").glob("*.json.gz"))) == len(FIXTURE_SYMBOLS)
    _forbid_generation(monkeypatch)
    second, second_funnels = _run(data_dir, cache_dir)
    assert second == first  # identical, including the stored trade ids
    assert second_funnels == first_funnels


@pytest.mark.parametrize("change", ["policy", "cost", "start", "end", "csv"])
def test_cache_misses_when_any_input_changes(data_dir, tmp_path, monkeypatch, change):
    cache_dir = tmp_path / "cache"
    local_data = write_fixture_csvs(tmp_path / "data")
    _run(local_data, cache_dir)
    kwargs = {}
    if change == "policy":
        kwargs["policy"] = apply_agent_threshold_overrides(
            get_policy_config(mode_id="core_srs", env={}),
            {"AGENT_THRESHOLDS": {"TECHNICAL": {"pullback_buffer_pips": 2.0}}},
        )
    elif change == "cost":
        kwargs["cost"] = replace(COST, slippage_pips=0.3)
    elif change == "start":
        kwargs["start"] = START + pd.Timedelta(hours=1)
    elif change == "end":
        kwargs["end"] = END - pd.Timedelta(hours=1)
    else:
        path = local_data / "GBPUSD_M15.csv"
        path.write_bytes(path.read_bytes() + b"\n")
    calls = []
    real = unified_replay.generate_candidates

    def _counting(symbol, *args, **kw):
        calls.append(symbol)
        return real(symbol, *args, **kw)

    monkeypatch.setattr(unified_replay, "generate_candidates", _counting)
    _run(local_data, cache_dir, **kwargs)
    expected = ["GBPUSD"] if change == "csv" else list(FIXTURE_SYMBOLS)
    assert calls == expected


def test_corrupt_cache_file_is_a_miss_not_an_error(data_dir, tmp_path):
    cache_dir = tmp_path / "cache"
    first, _ = _run(data_dir, cache_dir)
    entries = sorted((cache_dir / "candidates").glob("*.json.gz"))
    entries[0].write_bytes(b"not gzip")
    with gzip.open(entries[1], "wt", encoding="utf-8") as handle:
        handle.write('{"meta": {"kind": "other"}, "payload": {}}')
    again, _ = _run(data_dir, cache_dir)
    assert _comparable(again) == _comparable(first)
    # Both entries were rewritten and are valid hits now.
    for entry in entries:
        with gzip.open(entry, "rt", encoding="utf-8") as handle:
            assert handle.read().startswith("{")


def test_two_workers_match_in_process_and_leave_no_children(data_dir):
    serial, serial_funnels = _run(data_dir, None, workers=1)
    parallel, parallel_funnels = _run(data_dir, None, workers=2)
    assert _comparable(parallel) == _comparable(serial)
    assert parallel_funnels == serial_funnels
    assert multiprocessing.active_children() == []


def test_worker_exception_propagates_and_leaves_no_children(data_dir, tmp_path):
    broken = write_fixture_csvs(tmp_path / "broken")
    (broken / "AUDUSD_H1.csv").unlink()
    with pytest.raises(FileNotFoundError):
        _run(broken, None, workers=2)
    assert multiprocessing.active_children() == []


def test_parent_watchdog_exits_once_parent_changes():
    ppids = iter([100, 100, 1])
    exits = []
    unified_replay._parent_watchdog(100, getppid=lambda: next(ppids), exit=exits.append,
                                    interval=0.0, sleep=lambda _s: None)
    assert exits == [1]


def test_max_workers_env_forces_in_process_path(data_dir, monkeypatch):
    monkeypatch.setenv("FX_MAX_WORKERS", "1")
    assert unified_replay.effective_workers(6) == 1

    def _no_pool(*_args, **_kwargs):
        raise AssertionError("a process pool was started despite FX_MAX_WORKERS=1")

    monkeypatch.setattr(multiprocessing, "get_context", _no_pool)
    candidates, _ = _run(data_dir, None, workers=2)
    assert isinstance(candidates, list)


def test_effective_workers_ignores_invalid_cap():
    assert unified_replay.effective_workers(2, env={"FX_MAX_WORKERS": "x"}) == 2
    assert unified_replay.effective_workers(2, env={"FX_MAX_WORKERS": "0"}) == 2
    assert unified_replay.effective_workers(4, env={"FX_MAX_WORKERS": "2"}) == 2
    assert unified_replay.effective_workers(0, env={}) == 1


def test_cache_key_changes_with_code_fingerprint(monkeypatch):
    base = replay_cache.cache_key("candidates", symbol="GBPUSD")
    monkeypatch.setattr(replay_cache, "code_fingerprint", lambda *a, **k: "different")
    assert replay_cache.cache_key("candidates", symbol="GBPUSD") != base
