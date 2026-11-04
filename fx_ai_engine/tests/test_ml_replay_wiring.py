"""Replay candidates -> meta-labeler dataset, and the shadow runtime in replay (stage E3).

Fixture data only (tests/fixtures/live_bars_2026-11-03, GBPUSD and AUDUSD,
400 bars per timeframe); the only model fit is a tiny LogisticRegression on
that fixture's handful of trades.
"""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtesting.unified_replay import (
    SYMBOLS,
    CostModel,
    PointInTimeFeed,
    SimulationConfig,
    generate_all_candidates,
    load_csv_frames,
    simulate,
)
from config_microcapital import get_policy_config
from core.file_bridge_connection import FileBridgeConnection
from ml.features import FEATURE_ORDER
from ml.meta_labeler import replay_dataset
from ml.meta_labeler.replay_dataset import ReplayDatasetError, build_replay_dataset, train_from_replay_dataset
from ml.meta_labeler.shadow_runtime import (
    MetaLabelerShadowRuntime,
    ShadowFeatureBuildFailed,
    ShadowMarketDataUnavailable,
    ShadowRuntimeConfig,
    shadow_features,
)
from tests.replay_fixtures import FIXTURE_SYMBOLS, write_fixture_csvs
from tests.test_contract_engine import NOW_FIXED, _golden_bridge

POLICY = get_policy_config(mode_id="core_srs", env={})
START = pd.Timestamp("2026-10-30T00:00:00Z")
COST = CostModel()


class _ProbabilityModel:
    def __init__(self, probability: float):
        self.probability = probability

    def predict_proba(self, rows):
        return [[1.0 - self.probability, self.probability] for _ in rows]


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    data_dir = write_fixture_csvs(tmp_path_factory.mktemp("data"))
    frames = load_csv_frames(data_dir, symbols=FIXTURE_SYMBOLS)
    candidates, _ = generate_all_candidates(data_dir, POLICY, COST, symbols=FIXTURE_SYMBOLS, start=START, workers=1)
    result = simulate(candidates, frames, POLICY, COST, SimulationConfig(start=START))
    assert result.trades, "fixture should produce trades"
    return frames, candidates, result


def test_replay_dataset_has_one_row_per_trade_with_live_features(replay):
    frames, candidates, result = replay
    df = build_replay_dataset(candidates, result, frames)
    assert len(df) == len(result.trades)
    assert list(df.columns) == [*FEATURE_ORDER, "binary_label", "realized_r", "regime", "symbol", "strategy_id"]
    assert isinstance(df.index, pd.DatetimeIndex) and str(df.index.tz) == "UTC"
    assert not df[list(FEATURE_ORDER)].isna().any().any()
    trades = sorted(result.trades, key=lambda t: (pd.Timestamp(t["entry_time"]), SYMBOLS.index(t["symbol"])))
    for (ts, row), trade in zip(df.sort_index(kind="mergesort").iterrows(), trades):
        assert row["symbol"] == trade["symbol"]
        assert row["realized_r"] == pytest.approx(trade["r_multiple"])
        assert row["binary_label"] == int(trade["r_multiple"] > 0)
        # Parity: the same function the live shadow runtime calls, on the same closed-bar view.
        feed = PointInTimeFeed(frames)
        feed.set_time(ts)
        live = shadow_features(feed.fetch_ohlc, row["symbol"], row["regime"], ts.isoformat(), ShadowRuntimeConfig())
        np.testing.assert_array_equal(row[list(FEATURE_ORDER)].to_numpy(dtype=np.float32), live.values)


def test_replay_dataset_rejects_an_earlier_non_equal_candidate(replay):
    """A trade entering 15 min after its candidate's decision time is not matched to that candidate."""
    frames, candidates, result = replay
    trade = dict(result.trades[0])
    entry = pd.Timestamp(trade["entry_time"])
    trade["entry_time"] = (entry + pd.Timedelta(minutes=15)).isoformat()
    shifted = (trade["symbol"], pd.Timestamp(trade["entry_time"]))
    kept = [c for c in candidates if (c.symbol, pd.Timestamp(c.decision_time)) != shifted]
    assert any(c.symbol == trade["symbol"] and pd.Timestamp(c.decision_time) == entry for c in kept)
    with pytest.raises(ReplayDatasetError, match="no candidate"):
        build_replay_dataset(kept, SimpleNamespace(trades=[trade]), frames)


def test_replay_dataset_rejects_duplicate_exact_keys(replay):
    frames, candidates, result = replay
    trade = result.trades[0]
    match = next(c for c in candidates if c.symbol == trade["symbol"]
                 and pd.Timestamp(c.decision_time) == pd.Timestamp(trade["entry_time"]))
    duplicated = [*candidates, dataclasses.replace(match, strategy_id="other_strategy")]
    with pytest.raises(ReplayDatasetError, match="2 candidates"):
        build_replay_dataset(duplicated, result, frames)


@pytest.mark.parametrize("failure", [ShadowFeatureBuildFailed("nan feature"), ShadowMarketDataUnavailable("no bars")])
def test_replay_dataset_feature_failure_fails_the_build(replay, monkeypatch, failure):
    """A feature failure on any trade raises instead of shrinking the training set."""
    frames, candidates, result = replay
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == len(result.trades):
            raise failure
        return shadow_features(*args, **kwargs)

    monkeypatch.setattr(replay_dataset, "shadow_features", flaky)
    with pytest.raises(ReplayDatasetError, match="features not built"):
        build_replay_dataset(candidates, result, frames)


def test_replay_features_equal_live_bridge_features_at_the_same_decision_time(tmp_path):
    """The file bridge (live path) and the replay feed give the same shadow features at NOW_FIXED."""
    bridge_dir = _golden_bridge(tmp_path / "bridge")
    conn = FileBridgeConnection(bridge_dir, clock=lambda: NOW_FIXED)
    assert conn.connect(retry_count=1), conn.last_error
    frames = load_csv_frames(write_fixture_csvs(tmp_path / "data"), symbols=FIXTURE_SYMBOLS)
    feed = PointInTimeFeed(frames)
    decision_time = pd.Timestamp("2026-11-03T16:30:00Z")
    feed.set_time(decision_time)
    for symbol in FIXTURE_SYMBOLS:
        live = shadow_features(conn.fetch_ohlc_data, symbol, "TRENDING_BULL", NOW_FIXED.isoformat(),
                               ShadowRuntimeConfig())
        replayed = shadow_features(feed.fetch_ohlc, symbol, "TRENDING_BULL", decision_time.isoformat(),
                                   ShadowRuntimeConfig())
        assert live.timestamp == replayed.timestamp
        np.testing.assert_allclose(live.values, replayed.values, rtol=1e-6, atol=1e-7)


def test_tiny_fit_writes_a_checkpoint_the_shadow_runtime_loads(replay, tmp_path):
    frames, candidates, result = replay
    df = build_replay_dataset(candidates, result, frames)
    if df["binary_label"].nunique() < 2:  # pragma: no cover - depends on fixture outcomes
        rng = np.random.default_rng(7)
        df = pd.DataFrame(rng.normal(size=(40, len(FEATURE_ORDER))), columns=list(FEATURE_ORDER),
                          index=pd.date_range("2026-10-01", periods=40, freq="h", tz="UTC"))
        df["binary_label"] = np.arange(40) % 2
        df["realized_r"] = np.where(df["binary_label"] == 1, 1.0, -1.0)
        df["regime"], df["symbol"], df["strategy_id"] = "TRENDING_BULL", "GBPUSD", "trend_pullback_v1"
    checkpoint = tmp_path / "replay_baseline.joblib"
    summary = train_from_replay_dataset(df, checkpoint)
    assert summary["model_status"] == "fitted" and summary["n_rows"] == len(df)
    runtime = MetaLabelerShadowRuntime(
        config=ShadowRuntimeConfig(enabled=True, checkpoint_path=checkpoint), fetch_ohlc=lambda *a: pd.DataFrame()
    )
    assert runtime.model_loaded


def test_simulate_with_shadow_keeps_trades_and_logs_one_vote_per_approval(replay):
    frames, candidates, result = replay
    for probability, outcome in ((0.9, "PASS"), (0.1, "REJECT")):
        shadowed = simulate(candidates, frames, POLICY, COST,
                            SimulationConfig(start=START, shadow=ShadowRuntimeConfig(enabled=True)),
                            shadow_model=_ProbabilityModel(probability))
        assert shadowed.trades == result.trades
        assert shadowed.decisions == result.decisions
        assert len(shadowed.shadow_decisions) == result.decisions["APPROVED"]
        assert {d["outcome"] for d in shadowed.shadow_decisions} == {outcome}
        for vote in shadowed.shadow_decisions:
            assert vote["probability"] == pytest.approx(probability)
            assert (vote["would_be_risk"] == 0.0) is (outcome == "REJECT")
    assert result.shadow_decisions == []
    assert "shadow" not in result.config["simulation"]
