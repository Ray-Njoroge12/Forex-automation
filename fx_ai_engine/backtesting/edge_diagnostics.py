"""Separate signal quality from trading costs, against random and inverted controls (plan G2).

Usage (from fx_ai_engine/):
    python -m backtesting.edge_diagnostics --data-dir artifacts/datasets/mt5_2026-11-03

Every row uses the same candidate times, sizing fixed on the starting balance and
SRS account halts disabled, so expectancy measures the signal itself:
  * zero-cost rows show the gross edge of the entry rules;
  * RANDOM keeps entry times but picks the direction at random (should be ~0R gross);
  * INVERTED trades the opposite direction (if it beats the strategy, the rules
    are anti-predictive);
  * min-stop rows show how much of the loss comes from costs on tight stops.
The holdout period is excluded.
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.replay_report import trade_metrics
from backtesting.run_replay import DEFAULT_CACHE_DIR, install_sigterm_handler
from backtesting.unified_replay import (
    COST_SCENARIOS, SYMBOLS, CostModel, SimulationConfig, generate_all_candidates, load_csv_frames, simulate,
)
from config_microcapital import get_policy_config

ZERO_COST = CostModel("zero", 1.0, slippage_pips=0.0, commission_per_lot_usd=0.0, use_bar_spread=False,
                      spread_floor_pips={s: 0.0 for s in SYMBOLS})


def _row(label, result):
    m = trade_metrics(result.trades)
    if not m.get("trades"):
        return {"label": label, "trades": 0}
    return {"label": label, **{k: m[k] for k in ("trades", "win_rate", "expectancy_r", "expectancy_r_ci95",
                                                 "profit_factor", "avg_winner_r", "avg_loser_r")},
            "mean_cost_r": round(float(np.mean([t["commission_usd"] / t["risk_usd"] for t in result.trades])), 4)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Edge diagnostics with controls")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--start", default="2022-12-24")
    parser.add_argument("--holdout-months", type=int, default=6)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)
    install_sigterm_handler()
    cache_dir = None if args.no_cache else args.cache_dir.resolve()
    frames = load_csv_frames(args.data_dir)
    policy = get_policy_config(mode_id="core_srs", env={})
    policy.update({"DAILY_STOP_LOSS_PCT": 10.0, "WEEKLY_STOP_LOSS_PCT": 10.0,
                   "HARD_DRAWDOWN_PCT": 1e9, "LOSS_HALT_THRESHOLD": 10**6})
    start = pd.Timestamp(args.start, tz="UTC")
    end = min(frames[s]["M15"].index[-1] for s in SYMBOLS) - pd.DateOffset(months=args.holdout_months)
    candidates, _ = generate_all_candidates(args.data_dir, policy, COST_SCENARIOS["base"], start=start, end=end,
                                            cache_dir=cache_dir)
    cfg = lambda mode: SimulationConfig(exit_mode=mode, start=start, end=end, fixed_sizing=True)
    rng = random.Random(5)
    randomised = [replace(c, signal=dict(c.signal, direction=rng.choice(["BUY", "SELL"]))) for c in candidates]
    inverted = [replace(c, signal=dict(c.signal, direction="SELL" if c.signal["direction"] == "BUY" else "BUY"))
                for c in candidates]
    rows = []
    for label, cands, cost, mode in (
        ("strategy | zero cost | fixed 2.2R", candidates, ZERO_COST, "fixed_tp"),
        ("strategy | zero cost | live exits", candidates, ZERO_COST, "policy"),
        ("RANDOM direction | zero cost | fixed 2.2R", randomised, ZERO_COST, "fixed_tp"),
        ("INVERTED direction | zero cost | fixed 2.2R", inverted, ZERO_COST, "fixed_tp"),
        ("strategy | base cost | fixed 2.2R", candidates, COST_SCENARIOS["base"], "fixed_tp"),
        ("strategy | base cost | live exits", candidates, COST_SCENARIOS["base"], "policy"),
    ):
        rows.append(_row(label, simulate(cands, frames, policy, cost, cfg(mode))))
        print(rows[-1], flush=True)
    for min_stop in (15, 20):
        sub = [c for c in candidates if c.signal["stop_pips"] >= min_stop]
        rows.append(_row(f"strategy | base cost | live exits | stop>={min_stop} pips",
                         simulate(sub, frames, policy, COST_SCENARIOS["base"], cfg("policy"))))
        print(rows[-1], flush=True)
    stops = [c.signal["stop_pips"] for c in candidates]
    summary = {"period": [start.isoformat(), end.isoformat()], "candidates": len(candidates),
               "stop_pips_quartiles": [round(float(np.percentile(stops, q)), 2) for q in (25, 50, 75)], "rows": rows}
    out = args.out or args.data_dir.parent.parent / "replay" / "edge_diagnostics.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
