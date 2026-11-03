"""R1 bias audit: truncation, future corruption, warm-up parity and deviation on the live fixture."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtesting.unified_replay import COST_SCENARIOS
from research import bias_audit
from research.common import load_frames, policy_for_variant

FIXTURE = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
LIVE_DECISION = pd.Timestamp("2026-11-03T16:30:00Z")
COST = COST_SCENARIOS["base"]


@pytest.fixture(scope="module")
def frames():
    return load_frames(bridge_dir=FIXTURE)


@pytest.fixture(scope="module")
def policy():
    return policy_for_variant("baseline")[0]


@pytest.fixture(scope="module")
def gbp_times(frames, policy):
    sample = bias_audit.sample_decision_times(frames, policy, COST, "GBPUSD", max_decisions=8, seed=7)
    return sorted(set(sample["times"]) | {LIVE_DECISION})


def _leaky(frames, policy, cost, symbol, t, **_):
    # Reads the last row of whatever frame it is given, closed or not.
    return {"last_close": float(frames[symbol]["M15"].iloc[-1]["close"])}


def test_truncation_and_corruption_pass_on_live_decision_path(frames, policy, gbp_times):
    assert bias_audit.truncation_test(frames, policy, COST, "GBPUSD", gbp_times)["status"] == "PASS"
    for mode in ("random", "nan"):
        result = bias_audit.future_corruption_test(frames, policy, COST, "GBPUSD", gbp_times, mode=mode, seed=7)
        assert result["status"] == "PASS", result["examples"]
    fp = bias_audit.replay_decision(frames, policy, COST, "GBPUSD", LIVE_DECISION)
    assert fp["signal"]["direction"] == "BUY"
    assert fp["signal"]["stop_pips"] == pytest.approx(13.60, abs=0.01)
    assert len(fp["stage2"]) == 5


def test_leaky_decision_fails_truncation_and_random_corruption(frames, policy, gbp_times):
    times = [t for t in gbp_times if t < LIVE_DECISION][:3]
    assert times
    assert bias_audit.truncation_test(frames, policy, COST, "GBPUSD", times, decide=_leaky)["status"] == "FAIL"
    result = bias_audit.future_corruption_test(frames, policy, COST, "GBPUSD", times, mode="random", decide=_leaky)
    assert result["status"] == "FAIL"


def test_warmup_parity_observes_live_fetch_lengths(frames, policy):
    records = bias_audit.observe_fetch_lengths(frames, policy, COST, "GBPUSD", LIVE_DECISION)
    requested = {}
    for r in records:
        requested.setdefault(r["tf"], set()).add(r["requested"])
    for tf in ("H1", "H4", "M15"):
        assert requested[tf] == {350}
    ok = bias_audit.warmup_parity(frames, policy, COST, "GBPUSD", LIVE_DECISION)
    assert ok["status"] == "PASS"
    assert ok["live_fetch_bars"] == {"H1": 350, "H4": 350, "M15": 350}
    assert bias_audit.warmup_parity(frames, policy, COST, "GBPUSD", LIVE_DECISION, lookback_override=400)["status"] == "FAIL"


def test_warmup_deviation_is_inconclusive_on_short_real_frames(frames, policy):
    out = bias_audit.warmup_deviation(frames, policy, live_fetch_bars={"H1": 350, "H4": 350, "M15": 350})
    assert out["status"] == "INCONCLUSIVE"
    assert {e["status"] for e in out["frames"]} == {"INCONCLUSIVE"}
    assert out["synthetic"] == []


def test_synthetic_warmup_deviation_decreases_with_lookback_and_is_seeded(frames, policy):
    gbp = {"GBPUSD": frames["GBPUSD"]}
    kwargs = dict(live_fetch_bars={"H1": 350, "H4": 350, "M15": 350}, eval_points=30, synthetic_long_bars=2400, seed=7)
    first = bias_audit.warmup_deviation(gbp, policy, **kwargs)
    second = bias_audit.warmup_deviation(gbp, policy, **kwargs)
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(second, sort_keys=True, default=str)
    h1 = next(e for e in first["synthetic"] if e["tf"] == "H1")
    assert h1["source"] == "model_estimate"
    p95 = h1["indicators"]["ema_200"]["p95"]
    values = [p95[L] for L in sorted(p95)]
    assert all(b <= a + 1e-9 for a, b in zip(values, values[1:])), values
    assert first["status"] == "INCONCLUSIVE"  # synthetic results never decide the verdict


def test_cli_writes_report_and_returns_nonzero_when_inconclusive(tmp_path):
    code = bias_audit.main(["--bridge-dir", str(FIXTURE), "--max-decisions", "4", "--out", str(tmp_path)])
    report = json.loads((tmp_path / "r1_report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "INCONCLUSIVE"
    assert report["truncation"]["status"] == "PASS"
    assert code == 1


def test_corrupt_frames_only_touches_unclosed_bars(frames):
    t = LIVE_DECISION - pd.Timedelta(hours=6)
    altered = bias_audit.corrupt_frames(frames, t, "nan", seed=7)
    for tf, df in frames["GBPUSD"].items():
        closed = (df.index + pd.Timedelta(seconds=bias_audit.TIMEFRAME_SECONDS[tf])) <= t
        pd.testing.assert_frame_equal(altered["GBPUSD"][tf][closed], df[closed])
        assert np.isnan(altered["GBPUSD"][tf].loc[~closed, "close"]).all()
