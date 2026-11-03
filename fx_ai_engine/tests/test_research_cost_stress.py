"""R4 cost stress: commission handling, closed-form re-cost, fixture re-run and CLI outputs."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtesting.unified_replay import CostModel, normalise_frame
from research import cost_stress
from research.common import load_frames, policy_for_variant

FIXTURE = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
COST = CostModel(use_bar_spread=False, spread_floor_pips={"EURUSD": 1.0}, slippage_pips=0.5)


def _frame(start: str, periods: int, freq: str, prices: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return normalise_frame(pd.DataFrame({"open": prices, "high": prices + 0.0005, "low": prices - 0.0005,
                                         "close": prices, "volume": 1, "spread_points": 0}, index=idx))


def _frames():
    return {"EURUSD": {"M15": _frame("2026-01-05 07:00", 40, "15min", np.full(40, 1.1))}}


def _trade(direction: str, r: float, **extra) -> dict:
    t = dict(trade_id=f"T{direction}{r}", symbol="EURUSD", direction=direction,
             entry_time="2026-01-05T08:00:00+00:00", exit_time="2026-01-05T09:00:00+00:00",
             stop_pips=10.0, r_multiple=r, commission_usd=0.0, risk_usd=1000.0)
    t.update(extra)
    return t


def test_spread_slippage_cost_keeps_commission_at_1x():
    cost = cost_stress.SpreadSlippageCost("x", 2.0)
    base = CostModel()
    assert cost.spread_pips("EURUSD", 0) == pytest.approx(2 * base.spread_pips("EURUSD", 0))
    assert cost.slippage == pytest.approx(2 * base.slippage)
    assert cost.commission == 7.0


def test_recost_matches_closed_form():
    trades = [_trade("BUY", 1.0), _trade("SELL", -0.6)]
    # cost_pips = 1.0 spread + 2 x 0.5 slippage = 2 pips; 2 / 10 = 0.2 R per extra multiple.
    out = cost_stress.recost_trades(trades, _frames(), COST, (1.0, 1.5, 2.0, 3.0))
    assert [row["expectancy_r"] for row in out["rows"]] == pytest.approx([0.2, 0.1, 0.0, -0.2])
    assert out["breakeven_multiple"] == pytest.approx(2.0)
    assert out["negative_at_base"] is False


def test_recost_flags_negative_expectancy_at_base():
    out = cost_stress.recost_trades([_trade("BUY", -0.5), _trade("BUY", 0.2)], _frames(), COST, (1.0, 2.0))
    assert out["negative_at_base"] is True
    assert out["breakeven_multiple"] is None and out["breakeven_label"] == "<=1.0"


def test_scale_commission_changes_recost_only_through_commission():
    trades = [_trade("BUY", 1.0, commission_usd=70.0), _trade("SELL", -0.6, commission_usd=70.0)]
    plain = cost_stress.recost_trades(trades, _frames(), COST, (1.0, 2.0, 3.0))
    scaled = cost_stress.recost_trades(trades, _frames(), COST, (1.0, 2.0, 3.0), scale_commission=True)
    for p, s in zip(plain["rows"], scaled["rows"]):
        assert p["expectancy_r"] - s["expectancy_r"] == pytest.approx((p["multiple"] - 1) * 0.07, abs=1e-4)


STRESS_1_5X = CostModel("stress_1_5x", 1.5, use_bar_spread=False, spread_floor_pips={"EURUSD": 1.0},
                        slippage_pips=0.5)


def _stress_1_5x(trades: list[dict]) -> list[dict]:
    """The same trades as if replayed at stress_1_5x: s = 0.2 R and c = commission/risk per 1x, both scaled 1.5x."""
    out = []
    for t in trades:
        c = t["commission_usd"] / t["risk_usd"]
        out.append(dict(t, r_multiple=t["r_multiple"] - 0.5 * 0.2 - 0.5 * c, commission_usd=1.5 * t["commission_usd"]))
    return out


@pytest.mark.parametrize("scale_commission", [False, True])
def test_recost_of_stress_replay_uses_absolute_multiples(scale_commission):
    base_trades = [_trade("BUY", 1.0, commission_usd=70.0), _trade("SELL", -0.6, commission_usd=70.0)]
    stress_trades = _stress_1_5x(base_trades)
    multiples = (1.0, 1.5, 2.0, 3.0)
    base = cost_stress.recost_trades(base_trades, _frames(), COST, multiples, scale_commission=scale_commission)
    stress = cost_stress.recost_trades(stress_trades, _frames(), STRESS_1_5X, multiples,
                                       scale_commission=scale_commission)
    assert stress["replay_cost_multiplier"] == 1.5 and stress["replay_commission_multiplier"] == 1.5
    by_m = {row["multiple"]: row for row in stress["rows"]}
    if scale_commission:
        assert by_m[1.5]["expectancy_r"] == pytest.approx(np.mean([t["r_multiple"] for t in stress_trades]), abs=1e-4)
    assert [row["expectancy_r"] for row in stress["rows"]] == pytest.approx(
        [row["expectancy_r"] for row in base["rows"]], abs=1e-4)
    assert stress["breakeven_multiple"] == pytest.approx(base["breakeven_multiple"], abs=1e-4)
    assert by_m[1.0]["below_replay_cost"] and not by_m[2.0]["below_replay_cost"]


def test_fixture_cost_stress_table():
    frames = load_frames(bridge_dir=FIXTURE)
    report, trades = cost_stress.run_cost_stress(frames, policy_for_variant("baseline")[0])
    assert [row["multiple"] for row in report["recost"]["rows"]] == [1.0, 1.5, 2.0, 3.0]
    assert len(trades) == 19 and report["recost"]["trades"] == 19
    rerun = {row["multiple"]: row for row in report["rerun"]}
    assert rerun[1.0]["trades"] == 19
    assert rerun[3.0]["trades"] == 0 and rerun[3.0]["expectancy_r"] is None
    assert report["recost"]["negative_at_base"] is True
    assert any("stress_1_5x" in note for note in report["notes"])


def test_cli_writes_outputs(tmp_path):
    assert cost_stress.main(["--bridge-dir", str(FIXTURE), "--multiples", "1", "3", "--out", str(tmp_path)]) == 0
    for name in ("cost_stress.json", "cost_stress.md", "trades_1x.csv"):
        assert (tmp_path / name).exists()
    assert len(pd.read_csv(tmp_path / "trades_1x.csv")) == 19
