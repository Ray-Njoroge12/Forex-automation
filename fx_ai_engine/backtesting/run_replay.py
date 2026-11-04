"""Run the unified replay for one or more variants and cost scenarios (plan E5/E6).

Usage (from fx_ai_engine/):
    python -m backtesting.run_replay --data-dir artifacts/datasets/mt5_2026-11-03
    python -m backtesting.run_replay --data-dir ... --variants baseline coded_defaults pullback_2 \
        --exit-modes policy fixed_tp --costs base stress_1_5x stress_2x

The most recent --holdout-months are reported separately and are never used to
choose a variant. Every run, including rejected variants, is appended to
artifacts/experiments/registry.jsonl so that multiple testing is visible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtesting.replay_report import (
    breakdown, equity_metrics, monte_carlo_drawdown, render_markdown, trade_metrics, windows,
)
from backtesting.unified_replay import (
    COST_SCENARIOS, DEFAULT_WORKERS, EXIT_MODES, SYMBOLS, SimulationConfig, generate_all_candidates,
    load_csv_frames, simulate,
)
from config_microcapital import apply_agent_threshold_overrides, get_policy_config
from core.policy.envelope import canonical_policy_hash

PROJECT_ROOT = Path(__file__).parents[1]
REGISTRY = PROJECT_ROOT / "artifacts" / "experiments" / "registry.jsonl"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "artifacts" / "replay" / "cache"


def _raise_system_exit(signum, _frame) -> None:
    raise SystemExit(128 + signum)


def install_sigterm_handler() -> None:
    """Turn SIGTERM into SystemExit so worker pools are terminated on the way out."""
    signal.signal(signal.SIGTERM, _raise_system_exit)

# Ablations of the loosened live parameters against the coded defaults (assessment section 4.1).
VARIANTS: dict[str, dict] = {
    "baseline": {},  # active core_srs policy, as traded on the demo account
    "coded_defaults": {"AGENT_THRESHOLDS": {
        "TECHNICAL": {"pullback_buffer_pips": 2.0, "buy_rsi_min": 40.0, "buy_rsi_max": 65.0,
                      "sell_rsi_min": 35.0, "sell_rsi_max": 60.0},
        "REGIME": {"trend_distance_threshold": 0.0005, "adx_no_trade_below": 20.0, "adx_transition_below": 25.0}}},
    "pullback_2": {"AGENT_THRESHOLDS": {"TECHNICAL": {"pullback_buffer_pips": 2.0}}},
    "rsi_bounded": {"AGENT_THRESHOLDS": {"TECHNICAL": {"buy_rsi_min": 40.0, "buy_rsi_max": 65.0,
                                                       "sell_rsi_min": 35.0, "sell_rsi_max": 60.0}}},
    "adx_20_25": {"AGENT_THRESHOLDS": {"REGIME": {"adx_no_trade_below": 20.0, "adx_transition_below": 25.0}}},
    "trend_distance_5pip": {"AGENT_THRESHOLDS": {"REGIME": {"trend_distance_threshold": 0.0005}}},
}

ASSUMPTIONS = (
    "completed bars only; next-M15-open market fills; BID bars with ask = bid + max(recorded, floor) spread x cost multiplier; "
    "slippage 0.2 pips and $7/lot round-trip commission x multiplier; stop assumed before target within a bar; "
    "EA break-even/partial/trail applied from bar extremes, effective next bar; no calendar blackout, swap, or partial fills; "
    "loss-streak halt cleared at the next ISO week (operator review); candidates generated once with base costs."
)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _section(trades, equity, start_balance, label_range, risk):
    m = trade_metrics(trades)
    return {
        "range": label_range,
        "trades": m,
        "equity": equity_metrics(equity, start_balance) if not equity.empty else {},
        "by_symbol": breakdown(trades, "symbol"),
        "by_hour": breakdown(trades, "hour_utc"),
        "windows": windows(trades),
        "monte_carlo": monte_carlo_drawdown([t["r_multiple"] for t in trades], risk_per_trade=risk) if trades else {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unified replay runner")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--variants", nargs="+", default=["baseline"], choices=sorted(VARIANTS))
    parser.add_argument("--exit-modes", nargs="+", default=["policy"], choices=EXIT_MODES)
    parser.add_argument("--costs", nargs="+", default=["base"], choices=sorted(COST_SCENARIOS))
    parser.add_argument("--holdout-months", type=int, default=6)
    parser.add_argument("--start", default=None, help="ISO start date (default: data start + 60 days warm-up)")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="candidate worker processes (capped by FX_MAX_WORKERS)")
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS), choices=SYMBOLS)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="candidate cache keyed by policy, data, cost, range and code hashes")
    parser.add_argument("--no-cache", action="store_true", help="always regenerate candidates")
    parser.add_argument("--halts", choices=("srs", "edge"), default="srs",
                        help="srs: SRS daily/weekly/drawdown/loss-streak halts as specified; "
                             "edge: those account halts disabled so the full period measures strategy edge "
                             "(position limits, exposure cap, correlation and throttle unchanged)")
    args = parser.parse_args(argv)
    install_sigterm_handler()
    symbols = tuple(s for s in SYMBOLS if s in set(args.symbols))
    cache_dir = None if args.no_cache else args.cache_dir.resolve()

    data_dir = args.data_dir.resolve()
    datasets = sorted(p.name for p in data_dir.glob("*.csv"))
    dataset_hashes = {p.name: _sha(p) for p in sorted(data_dir.glob("*.csv"))}
    frames = load_csv_frames(data_dir, symbols=symbols)
    m15_start = max(frames[s]["M15"].index[0] for s in symbols)
    data_end = min(frames[s]["M15"].index[-1] for s in symbols) + pd.Timedelta(minutes=15)
    start = pd.Timestamp(args.start, tz="UTC") if args.start else m15_start + pd.Timedelta(days=60)
    holdout_start = data_end - pd.DateOffset(months=args.holdout_months)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = (args.out.resolve() if args.out else PROJECT_ROOT / "artifacts" / "replay" / run_id)
    out_root.mkdir(parents=True, exist_ok=True)
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    summary = []

    for variant in args.variants:
        policy = apply_agent_threshold_overrides(get_policy_config(mode_id="core_srs", env={}), VARIANTS[variant] or None)
        policy_hash = canonical_policy_hash(policy)
        print(f"[{variant}] generating candidates {start.date()} .. {data_end.date()} ...", flush=True)
        candidates, funnels = generate_all_candidates(data_dir, policy, COST_SCENARIOS["base"], symbols=symbols,
                                                      start=start, end=data_end, workers=args.workers,
                                                      cache_dir=cache_dir)
        print(f"[{variant}] {len(candidates)} candidates", flush=True)
        for exit_mode in args.exit_modes:
            for cost_name in args.costs:
                cost = COST_SCENARIOS[cost_name]
                sim_policy = dict(policy)
                if args.halts == "edge":
                    sim_policy.update({"DAILY_STOP_LOSS_PCT": 10.0, "WEEKLY_STOP_LOSS_PCT": 10.0,
                                       "HARD_DRAWDOWN_PCT": 1e9, "LOSS_HALT_THRESHOLD": 10**6})
                result = simulate(candidates, frames, sim_policy, cost, SimulationConfig(exit_mode=exit_mode, start=start, end=data_end, fixed_sizing=args.halts == "edge"))
                dev = [t for t in result.trades if pd.Timestamp(t["entry_time"]) < holdout_start]
                hold = [t for t in result.trades if pd.Timestamp(t["entry_time"]) >= holdout_start]
                eq = result.equity
                label = f"{variant}__{exit_mode}__{args.halts}"
                risk = float(policy["BASE_RISK_PCT"])
                report = {
                    "variant": label, "cost": cost_name, "generated_at": run_id, "policy_hash": policy_hash,
                    "datasets": datasets, "dataset_sha256_16": dataset_hashes, "assumptions": ASSUMPTIONS,
                    "holdout_start": holdout_start.isoformat(),
                    "development": _section(dev, eq[eq.index < holdout_start], 100_000.0,
                                            f"{start.date()} to {holdout_start.date()}", risk),
                    "holdout": _section(hold, eq[eq.index >= holdout_start], float(eq[eq.index < holdout_start]["balance"].iloc[-1]) if (eq.index < holdout_start).any() else 100_000.0,
                                        f"{holdout_start.date()} to {data_end.date()}", risk),
                    "decisions": dict(result.decisions), "risk_events": result.risk_events[:200],
                    "candidate_funnel": {s: dict(f) for s, f in funnels.items()},
                    "overrides": VARIANTS[variant],
                }
                stem = out_root / f"{label}__{cost_name}"
                stem.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
                stem.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
                pd.DataFrame(result.trades).to_csv(stem.with_name(stem.name + "_trades.csv"), index=False)
                d = report["development"]["trades"]
                h = report["holdout"]["trades"]
                row = {"run_id": run_id, "variant": label, "cost": cost_name, "policy_hash": policy_hash,
                       "datasets": dataset_hashes, "dev": {k: d.get(k) for k in ("trades", "win_rate", "expectancy_r", "profit_factor")},
                       "dev_max_dd": report["development"]["equity"].get("max_drawdown"),
                       "holdout": {k: h.get(k) for k in ("trades", "win_rate", "expectancy_r", "profit_factor")},
                       "report": str(stem.with_suffix(".md").relative_to(PROJECT_ROOT))}
                with REGISTRY.open("a", encoding="utf-8") as reg:
                    reg.write(json.dumps(row, default=str) + "\n")
                summary.append(row)
                print(f"  {label:34} {cost_name:12} dev n={d.get('trades')} E={d.get('expectancy_r')} PF={d.get('profit_factor')} "
                      f"DD={row['dev_max_dd']} | holdout n={h.get('trades')} E={h.get('expectancy_r')}", flush=True)

    (out_root / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"Reports: {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
