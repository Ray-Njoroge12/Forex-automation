"""R5: Monte Carlo of replay R-multiples through the SRS halts, with a risk-per-trade sweep.

Evidence for plan decision D3 (ADR 0001). No risk constant is changed by this
tool: every limit is read from config_microcapital and validation.validate_demo.

Path model (per resampled path of N trades):
  * i.i.d. draws, or a circular block bootstrap that keeps loss streaks together;
  * the same draws are reused for every risk level (common random numbers);
  * risk = risk_per_trade x LOSS_THROTTLE_STEPS[streak]; equity *= 1 + risk x r;
  * trades_per_day trades per day, 5 days per week; daily and weekly anchors
    reset at each new day and week;
  * after each trade, in HardRiskEngine order with >=: daily stop skips the rest
    of the day, weekly stop skips to the next week, drawdown halt ends the path,
    a loss streak at the halt threshold resets the streak and skips to the next
    week (the replay's operator reset). Skipped slots are not traded.

Usage (from fx_ai_engine/):
    USE_MT5_MOCK=1 ../.venv/bin/python -m research.monte_carlo --trades-csv <replay>_trades.csv --runs 10000
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from backtesting.replay_report import monte_carlo_drawdown
from config_microcapital import get_policy_config
from research.common import ARTIFACTS, run_id, write_json
from validation.validate_demo import MAX_DRAWDOWN

HEADER_NOTE = "Evidence for plan decision D3 (ADR 0001). No risk constant is changed by this tool."
SWEEP_RISKS = (0.010, 0.015, 0.020, 0.025, 0.030)  # research grid; BASE_RISK_PCT is appended from the policy
METHODS = ("iid", "block")
PERCENTILES = (5, 25, 50, 75, 95)
DAYS_PER_WEEK = 5


def srs_limits(policy: Mapping[str, object] | None = None) -> dict[str, Any]:
    policy = policy if policy is not None else get_policy_config(mode_id="core_srs", env={})
    return {
        "daily_stop_pct": float(policy["DAILY_STOP_LOSS_PCT"]),
        "weekly_stop_pct": float(policy["WEEKLY_STOP_LOSS_PCT"]),
        "hard_drawdown_pct": float(policy["HARD_DRAWDOWN_PCT"]),
        "loss_halt_threshold": int(policy["LOSS_HALT_THRESHOLD"]),
        "loss_throttle_steps": {int(k): float(v) for k, v in dict(policy["LOSS_THROTTLE_STEPS"]).items()},
        "base_risk_pct": float(policy["BASE_RISK_PCT"]),
        "acceptance_max_drawdown": float(MAX_DRAWDOWN),
    }


def default_risks(policy: Mapping[str, object]) -> list[float]:
    risks = list(SWEEP_RISKS)
    base = float(policy["BASE_RISK_PCT"])
    if base not in risks:
        risks.append(base)
    return risks


def resample(r: Sequence[float], n_trades: int, runs: int, method: str, block_length: int,
             rng: np.random.Generator) -> np.ndarray:
    r = np.asarray(r, dtype=float)
    n = r.size
    if method == "iid":
        return rng.choice(r, size=(runs, n_trades), replace=True)
    if method == "block":
        length = max(1, min(int(block_length), n))
        blocks = -(-n_trades // length)
        starts = rng.integers(0, n, size=(runs, blocks))
        idx = (starts[:, :, None] + np.arange(length)) % n
        return r[idx.reshape(runs, blocks * length)[:, :n_trades]]
    raise ValueError(f"unknown method {method!r}; expected one of {METHODS}")


def simulate_paths(draws: np.ndarray, risk: float, limits: Mapping[str, Any], trades_per_day: int) -> dict[str, np.ndarray]:
    runs, n = draws.shape
    threshold = int(limits["loss_halt_threshold"])
    throttle = np.array([limits["loss_throttle_steps"].get(k, 1.0) for k in range(threshold + 1)])
    eq = np.ones(runs)
    peak = np.ones(runs)
    day_anchor = np.ones(runs)
    week_anchor = np.ones(runs)
    streak = np.zeros(runs, dtype=int)
    resume_day = np.zeros(runs, dtype=int)
    stopped = np.zeros(runs, dtype=bool)
    max_dd = np.zeros(runs)
    hit = {k: np.zeros(runs, dtype=bool) for k in ("daily", "weekly", "drawdown", "loss_streak")}
    tpd = max(1, int(trades_per_day))
    for j in range(n):
        day, first_of_day = divmod(j, tpd)
        week = day // DAYS_PER_WEEK
        if first_of_day == 0:
            day_anchor = eq.copy()
            if day % DAYS_PER_WEEK == 0:
                week_anchor = eq.copy()
        active = ~stopped & (resume_day <= day)
        r = draws[:, j]
        risk_eff = risk * throttle[np.minimum(streak, threshold)]
        eq = np.where(active, eq * (1.0 + risk_eff * r), eq)
        streak = np.where(active & (r < 0), streak + 1, np.where(active & (r > 0), 0, streak))
        peak = np.maximum(peak, eq)
        dd = (peak - eq) / peak
        max_dd = np.maximum(max_dd, dd)
        daily = active & ((day_anchor - eq) / day_anchor >= limits["daily_stop_pct"])
        weekly = active & ((week_anchor - eq) / week_anchor >= limits["weekly_stop_pct"])
        dd_halt = active & (dd >= limits["hard_drawdown_pct"])
        streak_halt = active & (streak >= threshold)
        hit["daily"] |= daily
        hit["weekly"] |= weekly
        hit["drawdown"] |= dd_halt
        hit["loss_streak"] |= streak_halt
        stopped |= dd_halt
        resume_day = np.where(daily, np.maximum(resume_day, day + 1), resume_day)
        resume_day = np.where(weekly | streak_halt, np.maximum(resume_day, (week + 1) * DAYS_PER_WEEK), resume_day)
        streak = np.where(streak_halt, 0, streak)
    return {"final_equity": eq, "max_drawdown": max_dd, **{f"hit_{k}": v for k, v in hit.items()}}


def _pct(values: np.ndarray) -> dict[str, float]:
    return {f"p{p}": round(float(np.percentile(values, p)), 4) for p in PERCENTILES}


def infer_trades_per_day(exit_times: Sequence[Any]) -> int:
    if len(exit_times) == 0:
        return 1
    days = pd.to_datetime(pd.Series(list(exit_times)), utc=True).dt.strftime("%Y-%m-%d")
    return max(1, int(round(len(days) / days.nunique())))


def risk_sweep(r_values: Sequence[float], *, policy: Mapping[str, object] | None = None,
               risks: Sequence[float] | None = None, trade_counts: Sequence[int] = (30, 100),
               methods: Sequence[str] = METHODS, runs: int = 10000, block_length: int = 5,
               trades_per_day: int | None = None, exit_times: Sequence[Any] | None = None,
               seed: int = 11) -> dict[str, Any]:
    policy = policy if policy is not None else get_policy_config(mode_id="core_srs", env={})
    limits = srs_limits(policy)
    r = np.asarray([float(x) for x in r_values], dtype=float)
    if r.size == 0:
        raise ValueError("no R values to resample")
    risks = list(risks) if risks else default_risks(policy)
    tpd = int(trades_per_day) if trades_per_day else infer_trades_per_day(exit_times or [])
    rows = []
    for m_i, method in enumerate(methods):
        for n in trade_counts:
            rng = np.random.default_rng([seed, m_i, int(n)])
            draws = resample(r, int(n), runs, method, block_length, rng)
            for risk in risks:
                sim = simulate_paths(draws, float(risk), limits, tpd)
                rows.append({
                    "method": method, "risk": float(risk), "trades": int(n), "runs": runs,
                    "p_daily_stop": round(float(sim["hit_daily"].mean()), 4),
                    "p_weekly_stop": round(float(sim["hit_weekly"].mean()), 4),
                    "p_drawdown_halt": round(float(sim["hit_drawdown"].mean()), 4),
                    "p_loss_streak_halt": round(float(sim["hit_loss_streak"].mean()), 4),
                    "p_max_dd_gt_acceptance": round(float((sim["max_drawdown"] > limits["acceptance_max_drawdown"]).mean()), 4),
                    "max_drawdown": _pct(sim["max_drawdown"]),
                    "final_equity": _pct(sim["final_equity"]),
                })
    legacy = {str(n): monte_carlo_drawdown(r.tolist(), risk_per_trade=limits["base_risk_pct"], trades=int(n), runs=runs)
              for n in trade_counts}
    return {
        "tool": "r5_monte_carlo", "note": HEADER_NOTE, "limits": limits, "trades_in_sample": int(r.size),
        "mean_r": round(float(r.mean()), 4), "trades_per_day": tpd, "days_per_week": DAYS_PER_WEEK,
        "block_length": block_length, "runs": runs, "seed": seed, "risks": risks,
        "trade_counts": list(trade_counts), "rows": rows, "legacy_iid_reference": legacy,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lim = report["limits"]
    lines = [
        "# Monte Carlo risk sweep (R5)", "", report["note"], "",
        f"{report['trades_in_sample']} replay trades, mean R {report['mean_r']:+.4f}; {report['runs']} paths per row; "
        f"{report['trades_per_day']} trades/day, {report['days_per_week']} days/week; block length {report['block_length']}; "
        f"seed {report['seed']}.",
        f"SRS halts: daily {lim['daily_stop_pct']:.0%}, weekly {lim['weekly_stop_pct']:.0%}, drawdown {lim['hard_drawdown_pct']:.0%}, "
        f"{lim['loss_halt_threshold']} consecutive losses. Acceptance drawdown line (SRS 12.2): {lim['acceptance_max_drawdown']:.0%}.",
        "",
        "| method | risk | trades | P(daily) | P(weekly) | P(DD halt) | P(3-loss) | P(max DD > acceptance) | maxDD p5/p50/p95 | final p5/p50/p95 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        dd, fe = row["max_drawdown"], row["final_equity"]
        lines.append(
            f"| {row['method']} | {row['risk']:.2%} | {row['trades']} | {row['p_daily_stop']:.3f} | {row['p_weekly_stop']:.3f} | "
            f"{row['p_drawdown_halt']:.3f} | {row['p_loss_streak_halt']:.3f} | {row['p_max_dd_gt_acceptance']:.3f} | "
            f"{dd['p5']:.3f}/{dd['p50']:.3f}/{dd['p95']:.3f} | {fe['p5']:.3f}/{fe['p50']:.3f}/{fe['p95']:.3f} |")
    lines += ["", "Legacy i.i.d. reference (replay_report.monte_carlo_drawdown at BASE_RISK_PCT):", ""]
    for n, ref in report["legacy_iid_reference"].items():
        lines.append(f"- {n} trades: P(max DD > 15%) {ref.get('p_max_dd_gt_15pct')}, P(max DD > 20%) "
                     f"{ref.get('p_max_dd_gt_20pct')}, median max DD {ref.get('median_max_dd')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R5 Monte Carlo risk sweep over replay trades (offline)")
    parser.add_argument("--trades-csv", type=Path, required=True, help="replay trades CSV with r_multiple, exit_time")
    parser.add_argument("--runs", type=int, default=10000)
    parser.add_argument("--block-length", type=int, default=5)
    parser.add_argument("--risks", nargs="+", type=float, default=None)
    parser.add_argument("--trade-counts", nargs="+", type=int, default=[30, 100])
    parser.add_argument("--trades-per-day", type=int, default=None)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    trades = pd.read_csv(args.trades_csv).sort_values("exit_time")
    report = risk_sweep(trades["r_multiple"].tolist(), risks=args.risks, trade_counts=args.trade_counts,
                        runs=args.runs, block_length=args.block_length, trades_per_day=args.trades_per_day,
                        exit_times=trades["exit_time"].tolist(), seed=args.seed)
    report["trades_csv"] = str(args.trades_csv)
    out = Path(args.out or ARTIFACTS / "r5" / run_id())
    write_json(out / "monte_carlo.json", report)
    md = render_markdown(report)
    (out / "monte_carlo.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"Outputs: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
