"""Metrics, walk-forward windows and markdown for unified replay results (plan E5).

Monte Carlo for reports is ``research.monte_carlo.risk_sweep`` (R5); cost stress is
``research.cost_stress`` (R4). See ADR 0003.
"""
from __future__ import annotations

import math
import random
from collections import Counter
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

BREAKEVEN_BAND_R = 0.1  # |R| below this is reported as break-even, not a win or loss


def trade_metrics(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    r = np.array([t["r_multiple"] for t in trades], dtype=float)
    net = np.array([t["net_usd"] for t in trades], dtype=float)
    wins, losses = r[r >= BREAKEVEN_BAND_R], r[r <= -BREAKEVEN_BAND_R]
    gross_win, gross_loss = net[net > 0].sum(), -net[net < 0].sum()
    return {
        "trades": n,
        "win_rate": round(float((r >= BREAKEVEN_BAND_R).mean()), 4),
        "loss_rate": round(float((r <= -BREAKEVEN_BAND_R).mean()), 4),
        "breakeven_rate": round(float((np.abs(r) < BREAKEVEN_BAND_R).mean()), 4),
        "win_rate_srs_r_gt_0": round(float((r > 0).mean()), 4),
        "expectancy_r": round(float(r.mean()), 4),
        "expectancy_r_ci95": [round(float(x), 4) for x in _bootstrap_mean_ci(r)],
        "median_r": round(float(np.median(r)), 4),
        "avg_winner_r": round(float(wins.mean()), 4) if wins.size else 0.0,
        "avg_loser_r": round(float(losses.mean()), 4) if losses.size else 0.0,
        "profit_factor": round(float(gross_win / gross_loss), 4) if gross_loss > 0 else math.inf,
        "net_usd": round(float(net.sum()), 2),
        "max_consecutive_losses": _max_run(r <= -BREAKEVEN_BAND_R),
        "partial_close_rate": round(float(np.mean([bool(t["partial"]) for t in trades])), 4),
        "break_even_rate": round(float(np.mean([bool(t["break_even"]) for t in trades])), 4),
        "exit_reasons": dict(Counter(t["exit_reason"] for t in trades)),
    }


def _bootstrap_mean_ci(r: np.ndarray, n: int = 2000, seed: int = 7) -> tuple[float, float]:
    if r.size < 2:
        return (float(r.mean()), float(r.mean()))
    rng = np.random.default_rng(seed)
    means = rng.choice(r, size=(n, r.size), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def _max_run(mask: np.ndarray) -> int:
    best = run = 0
    for flag in mask:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best


def equity_metrics(equity: pd.DataFrame, starting_balance: float) -> dict[str, Any]:
    if equity.empty:
        return {}
    eq = equity["equity"]
    peak = eq.cummax()
    dd = (peak - eq) / peak
    years = max((eq.index[-1] - eq.index[0]).total_seconds() / (365.25 * 86400), 1e-9)
    final = float(equity["balance"].iloc[-1])
    return {
        "start": eq.index[0].isoformat(), "end": eq.index[-1].isoformat(),
        "final_balance": round(final, 2),
        "total_return": round(final / starting_balance - 1, 4),
        "cagr": round((final / starting_balance) ** (1 / years) - 1, 4) if final > 0 else -1.0,
        "max_drawdown": round(float(dd.max()), 4),
        "max_drawdown_at": dd.idxmax().isoformat(),
    }


def breakdown(trades: Sequence[Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list] = {}
    for t in trades:
        groups.setdefault(str(t[key]), []).append(t)
    out = {}
    for k, items in sorted(groups.items()):
        m = trade_metrics(items)
        out[k] = {x: m[x] for x in ("trades", "win_rate", "expectancy_r", "profit_factor", "net_usd") if x in m}
    return out


def windows(trades: Sequence[Mapping[str, Any]], months: int = 6) -> list[dict[str, Any]]:
    if not trades:
        return []
    frame = pd.DataFrame(trades)
    frame["exit"] = pd.to_datetime(frame["exit_time"], utc=True)
    period = frame["exit"].dt.tz_localize(None).dt.to_period(f"{months}M") if months != 6 else (
        frame["exit"].dt.year.astype(str) + "H" + ((frame["exit"].dt.month - 1) // 6 + 1).astype(str))
    rows = []
    for label, group in frame.groupby(period):
        m = trade_metrics(group.to_dict("records"))
        rows.append({"window": str(label), **{x: m[x] for x in ("trades", "win_rate", "expectancy_r", "profit_factor", "net_usd")}})
    return rows


def monte_carlo_drawdown(r_values: Sequence[float], *, risk_per_trade: float = 0.032, trades: int = 30,
                         runs: int = 20000, throttle: Mapping[int, float] | None = None,
                         seed: int = 11) -> dict[str, Any]:
    """Bootstrap the replay's realised R distribution through the SRS loss throttle.

    Kept only as R5's legacy i.i.d. reference kernel (``research.monte_carlo.risk_sweep``
    reports it as ``legacy_iid_reference``). Reports use ``risk_sweep``, which adds the
    block bootstrap, the SRS halts and the risk grid (ADR 0003).
    """
    r_values = [float(x) for x in r_values]
    if not r_values:
        return {}
    throttle = throttle or {0: 1.0, 1: 0.75, 2: 0.5}
    rng = random.Random(seed)
    max_dds, finals = [], []
    for _ in range(runs):
        eq = peak = 1.0
        streak, mdd = 0, 0.0
        for _ in range(trades):
            if streak >= 3:
                streak = 0  # operator reset after a halt, as in the replay
            r = rng.choice(r_values)
            eq *= 1 + risk_per_trade * throttle.get(streak, 0.5) * r
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak)
            streak = streak + 1 if r <= -BREAKEVEN_BAND_R else (0 if r >= BREAKEVEN_BAND_R else streak)
        max_dds.append(mdd)
        finals.append(eq)
    dds = np.array(max_dds)
    return {
        "risk_per_trade": risk_per_trade, "trades_per_run": trades, "runs": runs,
        "p_max_dd_gt_15pct": round(float((dds > 0.15).mean()), 4),
        "p_max_dd_gt_20pct": round(float((dds > 0.20).mean()), 4),
        "median_max_dd": round(float(np.median(dds)), 4),
        "median_final_equity": round(float(np.median(finals)), 4),
        "p_final_below_start": round(float((np.array(finals) < 1).mean()), 4),
    }


def _table(rows: Mapping[str, Mapping[str, Any]], key_name: str) -> str:
    if not rows:
        return "_none_\n"
    cols = list(next(iter(rows.values())).keys())
    lines = [f"| {key_name} | " + " | ".join(cols) + " |", "|" + " --- |" * (len(cols) + 1)]
    for k, v in rows.items():
        lines.append(f"| {k} | " + " | ".join(str(v.get(c, "")) for c in cols) + " |")
    return "\n".join(lines) + "\n"


def render_markdown(report: Mapping[str, Any]) -> str:
    out = [f"# Replay report: {report['variant']} / {report['cost']}", ""]
    out.append(f"Generated {report['generated_at']}. Policy hash `{report['policy_hash'][:12]}`. "
               f"Datasets: {', '.join(report['datasets'])}.")
    out.append("")
    out.append("Assumptions: " + report["assumptions"])
    out.append("")
    for part in ("development", "holdout"):
        sec = report.get(part)
        if not sec:
            continue
        out.append(f"## {part.title()} ({sec['range']})")
        m, e = sec["trades"], sec["equity"]
        if m.get("trades", 0) == 0:
            out.append("No trades.\n")
            continue
        out.append(
            f"- Trades {m['trades']}, win rate {m['win_rate']:.1%} (SRS r>0: {m['win_rate_srs_r_gt_0']:.1%}), "
            f"break-even {m['breakeven_rate']:.1%}\n"
            f"- Expectancy {m['expectancy_r']:+.3f}R (95% CI {m['expectancy_r_ci95'][0]:+.3f} to {m['expectancy_r_ci95'][1]:+.3f}), "
            f"profit factor {m['profit_factor']}, avg winner {m['avg_winner_r']:.2f}R, avg loser {m['avg_loser_r']:.2f}R\n"
            f"- Return {e.get('total_return', 0):+.1%}, CAGR {e.get('cagr', 0):+.1%}, max drawdown {e.get('max_drawdown', 0):.1%}, "
            f"longest losing run {m['max_consecutive_losses']}\n"
            f"- Exits: {m['exit_reasons']}\n")
        out.append("### By symbol\n" + _table(sec["by_symbol"], "symbol"))
        out.append("### By entry hour (UTC)\n" + _table(sec["by_hour"], "hour"))
        out.append("### Walk-forward windows (half-years)\n")
        out.append(_table({w["window"]: {k: v for k, v in w.items() if k != "window"} for w in sec["windows"]}, "window"))
        cs = sec.get("cost_stress") or {}
        if cs.get("rows") and not cs.get("no_trades"):
            levels = ", ".join(f"{row['multiple']:g}x {row['expectancy_r']:+.3f}R" for row in cs["rows"])
            out.append(f"### Cost stress (R4, fixed trade set, spread+slippage+commission scaled)\n"
                       f"Expectancy {levels}; break-even multiple {cs['breakeven_label']}\n")
        mc = sec.get("monte_carlo") or {}
        if mc.get("rows"):
            base = mc["limits"]["base_risk_pct"]
            out.append(f"### Monte Carlo (R5 resample of realised R at risk {base:.1%}; full risk sweep in the JSON)\n")
            out.append("| method | trades | P(DD halt) | P(loss-streak halt) | P(max DD > acceptance) | median max DD |\n"
                       "| --- | --- | --- | --- | --- | --- |")
            for row in (r for r in mc["rows"] if r["risk"] == base):
                out.append(f"| {row['method']} | {row['trades']} | {row['p_drawdown_halt']:.3f} | "
                           f"{row['p_loss_streak_halt']:.3f} | {row['p_max_dd_gt_acceptance']:.3f} | "
                           f"{row['max_drawdown']['p50']:.1%} |")
            out.append("")
    out.append("## Decision funnel\n" + _table({k: {"count": v} for k, v in report["decisions"].items()}, "outcome"))
    out.append("## Candidate funnel (stage 1)\n")
    for sym, f in report["candidate_funnel"].items():
        top = ", ".join(f"{k}={v}" for k, v in sorted(f.items(), key=lambda kv: -kv[1])[:6])
        out.append(f"- {sym}: {top}")
    return "\n".join(out) + "\n"
