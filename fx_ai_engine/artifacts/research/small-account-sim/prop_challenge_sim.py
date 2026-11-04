"""Prop-firm challenge pass probability for the current strategy (analysis script, not engine code).

Resamples the strategy's replay trades by trading day and applies an FTMO-style rule set: profit target,
5% daily loss (from start-of-day equity), 10% static max loss. Run from fx_ai_engine/:

    ../.venv/bin/python artifacts/research/small-account-sim/prop_challenge_sim.py

Limits: ignores the engine's own halts (3-loss halt, SRS daily/weekly stops), uses the replay's ~7 trades per
day, and the replay is a no-edge strategy (-0.35R per trade after costs). Indicative only.
"""
import numpy as np
import pandas as pd

df = pd.read_csv("artifacts/replay/2026-11-03_edge/baseline__policy__edge__base_trades.csv", parse_dates=["exit_time"])
df = df.sort_values("exit_time")
df["day"] = df["exit_time"].dt.date
days = [g["r_multiple"].to_numpy() for _, g in df.groupby("day")]
n_days = len(days)
print(f"trades {len(df)}, trading days {n_days}, trades/day {len(df) / n_days:.1f}, mean R {df.r_multiple.mean():+.3f}")
rng = np.random.default_rng(42)


def run(day_list, risk, target, max_days, sims, daily=0.05, maxloss=0.10):
    res = {"pass": 0, "daily": 0, "maxloss": 0, "timeout": 0}
    for _ in range(sims):
        eq = 1.0
        outcome = "timeout"
        for _d in range(max_days):
            rs = day_list[rng.integers(len(day_list))]
            start = eq
            done = False
            for r in rs:
                eq += risk * r
                if eq <= 1 - maxloss:
                    outcome, done = "maxloss", True
                    break
                if start - eq >= daily:
                    outcome, done = "daily", True
                    break
            if done:
                break
            if eq >= 1 + target:
                outcome = "pass"
                break
        res[outcome] += 1
    return {k: v / sims for k, v in res.items()}


print("\nPhase 1 (+10% target, 5% daily, 10% static max loss)")
print("risk/trade | horizon | P(pass) P(daily breach) P(max-loss breach) P(timeout)")
for risk in (0.005, 0.01, 0.015, 0.032):
    for horizon in (30, 120):
        r = run(days, risk, 0.10, horizon, 6000)
        print(f"  {risk * 100:4.1f}%   | {horizon:3d} days | {r['pass']:.3f}   {r['daily']:.3f}   {r['maxloss']:.3f}   {r['timeout']:.3f}")

print("\nBoth phases (10% then 5%), 120-day horizon each")
for risk in (0.005, 0.01):
    p1 = run(days, risk, 0.10, 120, 6000)["pass"]
    p2 = run(days, risk, 0.05, 120, 6000)["pass"]
    print(f"  risk {risk * 100:.1f}%: P(phase1) {p1:.3f} x P(phase2) {p2:.3f} = {p1 * p2:.4f}")

print("\nControl: same trades shifted to ~zero expectancy (+0.35R each)")
shifted = [d + 0.35 for d in days]
for risk in (0.005, 0.01):
    r = run(shifted, risk, 0.10, 120, 6000)
    print(f"  risk {risk * 100:.1f}%, 120 days: P(pass) {r['pass']:.3f}, daily {r['daily']:.3f}, maxloss {r['maxloss']:.3f}, timeout {r['timeout']:.3f}")
