"""R4: cost stress of replay trades at multiples of spread plus slippage.

Two views per multiple m:
  * re-cost: the replayed trade set is kept fixed and each trade's R is moved
    from the replay's own cost multiple m0 to m, R(m) = r - (m - m0) * cost_pips / stop_pips,
    where cost_pips = spread at 1x + 2 * slippage at 1x. Multiples are absolute
    (1x = base cost) whatever profile the replay ran at. This isolates the cost
    effect and gives the break-even multiple in closed form.
  * re-run: the replay's stage 2 is re-simulated with the costs scaled, so the
    adversarial spread filter and sizing react (selection effect included).
Commission stays at 1x unless --scale-commission is given (run_replay's
CostModel scales spread, slippage and commission together).

Usage (from fx_ai_engine/):
    USE_MT5_MOCK=1 ../.venv/bin/python -m research.cost_stress \
        --bridge-dir tests/fixtures/live_bars_2026-11-03 --multiples 1 1.5 2 3
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from backtesting.replay_report import trade_metrics
from backtesting.unified_replay import (
    COST_SCENARIOS, EXIT_MODES, Candidate, CostModel, Frames, SimulationConfig, pip_size, simulate,
)
from core.policy.envelope import canonical_policy_hash
from research.common import (
    ARTIFACTS, add_source_args, dataset_hashes, load_frames, parse_ts, policy_for_variant, run_id,
    run_replay_in_process, source_dir, write_json,
)

DEFAULT_MULTIPLES = (1.0, 1.5, 2.0, 3.0)
NOTES = [
    "run_replay already provides cost profiles base (1.0x), stress_1_5x (1.5x) and stress_2x (2.0x) through "
    "unified_replay.COST_SCENARIOS; each scales spread, slippage and commission together and re-runs stage 2 "
    "with candidates generated once at base cost, one report per profile.",
    "Built here because it was missing: a 3x multiple; a stress of spread plus slippage with commission held at 1x; "
    "a fixed-trade-set re-cost that separates the cost effect from the selection effect (the adversarial spread "
    "filter drops entries as spreads widen); the cost multiple at which expectancy reaches zero; and one table "
    "across multiples.",
    "Re-cost model: R(m) = r - (m - m0) * (spread_1x + 2 * slippage_1x) / stop_pips, where m0 is the cost multiple "
    "the trades were replayed at (1.0 for base, 1.5 for stress_1_5x) and multiples are absolute. Spread is taken "
    "from the entry bar for BUY (ask paid at entry) and from the exit bar for SELL (ask paid at exit). Exact under "
    "this linear model; approximate for trades with a partial close, where part of the position exits at a "
    "different bar. Rows below m0 (below_replay_cost) add cost back out of the realised trades.",
    "Commission: with commission_1x = commission_usd / k, where k is the commission multiple the replay applied "
    "(1.5 for stress_1_5x), the re-cost holds commission at 1x by default (subtracting (1 - k) * commission_1x / "
    "risk_usd) and with --scale-commission sets it to m (subtracting (m - k) * commission_1x / risk_usd), "
    "matching run_replay's convention.",
]


@dataclass(frozen=True)
class SpreadSlippageCost(CostModel):
    """Cost model that scales spread and slippage by ``multiplier`` but keeps commission at 1x."""

    @property
    def commission(self) -> float:
        return self.commission_per_lot_usd


def scaled_cost(cost: CostModel, multiple: float, *, scale_commission: bool = False) -> CostModel:
    cls = CostModel if scale_commission else SpreadSlippageCost
    return cls(name=f"x{multiple:g}", multiplier=float(multiple), slippage_pips=cost.slippage_pips,
               commission_per_lot_usd=cost.commission_per_lot_usd, use_bar_spread=cost.use_bar_spread,
               spread_floor_pips=dict(cost.spread_floor_pips))


def trade_cost_pips(trade: Mapping[str, Any], frames: Frames, cost: CostModel) -> float:
    """Spread + 2 x slippage at 1x for one trade, in pips."""
    base = replace(cost, multiplier=1.0)
    symbol = trade["symbol"]
    if trade["direction"] == "BUY":
        bar_start = pd.Timestamp(trade["entry_time"])
    else:
        bar_start = pd.Timestamp(trade["exit_time"]) - pd.Timedelta(minutes=15)
    # A missing bar falls back to the spread floor (recorded spread 0).
    points = float(frames[symbol]["M15"]["spread_points"].get(bar_start, 0.0))
    return base.spread_pips(symbol, points) + 2.0 * base.slippage_pips


def _commission_scale(cost: CostModel) -> float:
    """Factor the replay applied to the 1x commission (cost.multiplier for CostModel, 1 for SpreadSlippageCost)."""
    return cost.commission / cost.commission_per_lot_usd if cost.commission_per_lot_usd > 0 else 1.0


def recost_trades(trades: Sequence[Mapping[str, Any]], frames: Frames, cost: CostModel,
                  multiples: Sequence[float] = DEFAULT_MULTIPLES, *, scale_commission: bool = False) -> dict[str, Any]:
    """Re-cost a fixed trade set to absolute cost multiples.

    ``cost`` must be the cost model the trades were replayed with: their realised R already
    includes spread and slippage at ``cost.multiplier`` and commission at ``_commission_scale(cost)``,
    so R(m) = r - (m - cost.multiplier) * s - (q(m) - k) * c, with s and c per 1x and the target
    commission multiple q(m) = m with ``scale_commission`` and 1 without (commission held at 1x).
    """
    m0, k = float(cost.multiplier), _commission_scale(cost)
    out: dict[str, Any] = {"model": "fixed_trade_set_linear", "scale_commission": scale_commission,
                           "trades": len(trades), "replay_cost": cost.name, "replay_cost_multiplier": m0,
                           "replay_commission_multiplier": k}
    if not trades:
        out.update(rows=[{"multiple": float(m), "trades": 0, "expectancy_r": None, "below_replay_cost": float(m) < m0}
                         for m in multiples],
                   breakeven_multiple=None, breakeven_label="no trades", negative_at_base=None, no_trades=True)
        return out
    r = np.array([float(t["r_multiple"]) for t in trades])
    s = np.array([trade_cost_pips(t, frames, cost) / float(t["stop_pips"]) for t in trades])
    c = np.array([float(t["commission_usd"]) / k / float(t["risk_usd"]) if float(t["risk_usd"]) > 0 else 0.0
                  for t in trades])

    def at(m: float) -> np.ndarray:
        return r - (m - m0) * s - ((m if scale_commission else 1.0) - k) * c

    rows = [{"multiple": float(m), "trades": len(trades), "expectancy_r": round(float(at(m).mean()), 4),
             "below_replay_cost": float(m) < m0} for m in multiples]
    mean_r, mean_s, mean_c = float(r.mean()), float(s.mean()), float(c.mean())
    # mean R(m) = a - slope * m
    if scale_commission:
        a, slope = mean_r + m0 * mean_s + k * mean_c, mean_s + mean_c
    else:
        a, slope = mean_r + m0 * mean_s + (k - 1.0) * mean_c, mean_s
    at_1x = float(at(1.0).mean())
    if slope > 0:
        be = a / slope  # mean R(be) == 0
        if be > 1.0:
            out.update(breakeven_multiple=round(be, 4), breakeven_label=f"{be:.2f}x", negative_at_base=False)
        else:
            out.update(breakeven_multiple=None, breakeven_label="<=1.0", negative_at_base=True)
    else:
        out.update(breakeven_multiple=None, breakeven_label="undefined (zero cost)", negative_at_base=at_1x <= 0)
    out.update(rows=rows, mean_r_realised=round(mean_r, 4), negative_at_replay_cost=mean_r <= 0,
               mean_cost_r_per_multiple=round(slope, 4), no_trades=False)
    return out


def rerun_trades(candidates: Sequence[Candidate], frames: Frames, policy: Mapping[str, object], cost: CostModel,
                 multiples: Sequence[float] = DEFAULT_MULTIPLES, *, exit_mode: str = "policy", start=None, end=None,
                 scale_commission: bool = False) -> list[dict[str, Any]]:
    rows = []
    for m in multiples:
        result = simulate(candidates, frames, policy, scaled_cost(cost, m, scale_commission=scale_commission),
                          SimulationConfig(exit_mode=exit_mode, start=start, end=end))
        metrics = trade_metrics(result.trades)
        row = {"multiple": float(m), "trades": metrics["trades"]}
        for k in ("expectancy_r", "expectancy_r_ci95", "profit_factor", "net_usd"):
            row[k] = metrics.get(k)
        rows.append(row)
    return rows


def run_cost_stress(frames: Frames, policy: Mapping[str, object], *, multiples: Sequence[float] = DEFAULT_MULTIPLES,
                    cost: CostModel = COST_SCENARIOS["base"], exit_mode: str = "policy", start=None, end=None,
                    scale_commission: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (report, base trades). Base trades are the 1x replay used for the re-cost."""
    candidates, _, base = run_replay_in_process(frames, policy, cost, exit_mode=exit_mode, start=start, end=end)
    report = {
        "tool": "r4_cost_stress", "policy_hash": canonical_policy_hash(policy), "exit_mode": exit_mode,
        "base_cost": cost.name, "multiples": [float(m) for m in multiples], "scale_commission": scale_commission,
        "recost": recost_trades(base.trades, frames, cost, multiples, scale_commission=scale_commission),
        "rerun": rerun_trades(candidates, frames, policy, cost, multiples, exit_mode=exit_mode, start=start, end=end,
                              scale_commission=scale_commission),
        "notes": NOTES,
    }
    return report, base.trades


def render_markdown(report: Mapping[str, Any]) -> str:
    rc = report["recost"]
    rerun = {row["multiple"]: row for row in report["rerun"]}
    lines = [f"# Cost stress (R4): {report.get('variant', '')} / {report['exit_mode']}", "",
             f"Policy hash `{report['policy_hash'][:12]}`. Commission scaled: {report['scale_commission']}. "
             f"Re-cost input replayed at {rc.get('replay_cost_multiplier', 1.0):g}x (multiples are absolute).", "",
             f"Break-even cost multiple (re-cost, fixed trade set): **{rc['breakeven_label']}**"
             + (" (expectancy is already <= 0 at 1x)" if rc.get("negative_at_base") else ""), "",
             "| multiple | re-cost trades | re-cost expectancy R | re-run trades | re-run expectancy R | re-run CI95 | re-run PF | re-run net USD |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rc["rows"]:
        rr = rerun.get(row["multiple"], {})
        lines.append(f"| {row['multiple']:g}x | {row['trades']} | {row['expectancy_r']} | {rr.get('trades')} | "
                     f"{rr.get('expectancy_r')} | {rr.get('expectancy_r_ci95')} | {rr.get('profit_factor')} | {rr.get('net_usd')} |")
    lines += ["", "## Notes", ""] + [f"- {n}" for n in report["notes"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R4 cost stress: re-cost and re-run replay trades (offline)")
    add_source_args(parser)
    parser.add_argument("--multiples", nargs="+", type=float, default=list(DEFAULT_MULTIPLES))
    parser.add_argument("--exit-mode", default="policy", choices=EXIT_MODES)
    parser.add_argument("--scale-commission", action="store_true")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    frames = load_frames(bridge_dir=args.bridge_dir, data_dir=args.data_dir, symbols=args.symbols)
    policy, _ = policy_for_variant(args.variant)
    report, trades = run_cost_stress(frames, policy, multiples=args.multiples, exit_mode=args.exit_mode,
                                     start=parse_ts(args.start), end=parse_ts(args.end),
                                     scale_commission=args.scale_commission)
    report.update(variant=args.variant, source=str(source_dir(args)),
                  dataset_sha256_16=dataset_hashes(source_dir(args)))
    out = Path(args.out or ARTIFACTS / "r4" / run_id())
    write_json(out / "cost_stress.json", report)
    md = render_markdown(report)
    (out / "cost_stress.md").write_text(md, encoding="utf-8")
    pd.DataFrame(trades).to_csv(out / "trades_1x.csv", index=False)
    print(md)
    print(f"Outputs: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
