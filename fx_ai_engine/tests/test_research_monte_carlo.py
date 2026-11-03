"""R5 Monte Carlo: SRS limits read from config, block bootstrap, halt rules and reproducibility."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from config_microcapital import get_policy_config
from research import monte_carlo
from validation.validate_demo import MAX_DRAWDOWN

POLICY = get_policy_config(mode_id="core_srs", env={})
BASE = float(POLICY["BASE_RISK_PCT"])


def _row(report, **match):
    return next(r for r in report["rows"] if all(r[k] == v for k, v in match.items()))


def test_limits_come_from_policy_and_acceptance_line():
    lim = monte_carlo.srs_limits(POLICY)
    assert lim["daily_stop_pct"] == POLICY["DAILY_STOP_LOSS_PCT"]
    assert lim["weekly_stop_pct"] == POLICY["WEEKLY_STOP_LOSS_PCT"]
    assert lim["hard_drawdown_pct"] == POLICY["HARD_DRAWDOWN_PCT"]
    assert lim["loss_halt_threshold"] == POLICY["LOSS_HALT_THRESHOLD"]
    assert lim["loss_throttle_steps"] == dict(POLICY["LOSS_THROTTLE_STEPS"])
    assert lim["acceptance_max_drawdown"] == MAX_DRAWDOWN


def test_default_risks_end_at_policy_base_risk():
    risks = monte_carlo.default_risks(POLICY)
    assert risks[0] == pytest.approx(0.010)
    assert risks[-1] == BASE


def test_block_draws_with_full_length_are_circular_slices():
    r = np.arange(7, dtype=float)
    draws = monte_carlo.resample(r, 7, 50, "block", 7, np.random.default_rng(3))
    for row in draws:
        start = int(row[0])
        assert list(row) == [float((start + k) % 7) for k in range(7)]


def test_compounded_losses_reach_daily_stop():
    # 3.84% + 2.88% + 1.92% (throttled) compounds to an 8.4% daily loss.
    out = monte_carlo.risk_sweep([-1.2], policy=POLICY, risks=[BASE], trade_counts=(30,), runs=50, trades_per_day=10)
    assert _row(out, method="iid")["p_daily_stop"] == 1.0
    assert _row(out, method="block")["p_daily_stop"] == 1.0


def test_throttle_keeps_minus_one_r_streak_under_daily_stop():
    # 3.2% + 2.4% + 1.6% compounds to 7.03% < 8%; the third loss trips the streak halt.
    out = monte_carlo.risk_sweep([-1.0], policy=POLICY, risks=[BASE], trade_counts=(30,), runs=50, trades_per_day=10)
    row = _row(out, method="iid")
    assert row["p_daily_stop"] == 0.0
    assert row["p_loss_streak_halt"] == 1.0
    assert row["max_drawdown"]["p50"] == pytest.approx(1 - 0.968 * 0.976 * 0.984, abs=1e-4)


def test_probabilities_and_percentiles_are_well_formed():
    rng = np.random.default_rng(0)
    r = list(rng.normal(0.05, 1.2, size=40))
    out = monte_carlo.risk_sweep(r, policy=POLICY, runs=500, trades_per_day=3)
    assert len(out["rows"]) == 2 * len(monte_carlo.default_risks(POLICY)) * 2
    for row in out["rows"]:
        for key in ("p_daily_stop", "p_weekly_stop", "p_drawdown_halt", "p_loss_streak_halt", "p_max_dd_gt_acceptance"):
            assert 0.0 <= row[key] <= 1.0
        for band in ("max_drawdown", "final_equity"):
            values = [row[band][f"p{p}"] for p in monte_carlo.PERCENTILES]
            assert values == sorted(values)
    assert set(out["legacy_iid_reference"]) == {"30", "100"}


def test_same_seed_same_output():
    r = [1.5, -1.0, -1.0, 0.4, 2.2, -1.1]
    a = monte_carlo.risk_sweep(r, policy=POLICY, runs=300, trades_per_day=2, seed=5)
    b = monte_carlo.risk_sweep(r, policy=POLICY, runs=300, trades_per_day=2, seed=5)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_cli_writes_outputs(tmp_path):
    csv = tmp_path / "trades.csv"
    pd.DataFrame({"r_multiple": [1.2, -1.0, 0.3, -1.1, 2.0, -0.4, 0.8, -1.0],
                  "exit_time": pd.date_range("2026-01-05", periods=8, freq="6h", tz="UTC").astype(str)}).to_csv(csv, index=False)
    out = tmp_path / "out"
    assert monte_carlo.main(["--trades-csv", str(csv), "--runs", "200", "--out", str(out)]) == 0
    report = json.loads((out / "monte_carlo.json").read_text(encoding="utf-8"))
    assert report["trades_per_day"] == 4  # 8 exits over 2 UTC days
    assert (out / "monte_carlo.md").read_text(encoding="utf-8").startswith("# Monte Carlo risk sweep (R5)")
