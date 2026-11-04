"""Run the unified replay for one or more variants and cost scenarios (plan E5/E6).

Usage (from fx_ai_engine/):
    python -m backtesting.run_replay --data-dir artifacts/datasets/mt5_2026-11-03
    python -m backtesting.run_replay --data-dir ... --variants baseline coded_defaults pullback_2 \
        --exit-modes policy fixed_tp --costs base stress_1_5x stress_2x

The most recent --holdout-months are reported separately and are never used to
choose a variant. Every run, including rejected variants, is appended to
artifacts/experiments/registry.jsonl so that multiple testing is visible.

Every result goes through the R2 authenticity gate (research.authenticity_gate,
with the R1 bias audit computed once per policy and dataset and cached). The
gate verdict is written next to the report as <stem>_gate.json, the report's
first line says whether it is evidence, and the registry row carries
gate_verdict and evidence_status. --no-gate skips R1/R2 and labels the report
NOT EVIDENCE (ADR 0003).

--strategy selects a registered strategy (core.strategy, ADR 0014; default
trend_pullback_v1, the live strategy) and --strategy-params a JSON object of
its parameters. Variants are threshold overrides of the default rules, so a
non-default strategy accepts only --variants baseline. Strategy id and params go
into the candidate and R1 cache keys, the report and the registry row; reports
for a strategy that is not live-eligible say so on their second line.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtesting import replay_cache
from backtesting.replay_report import (
    breakdown, equity_metrics, render_markdown, trade_metrics, windows,
)
from backtesting.unified_replay import (
    COST_SCENARIOS, DEFAULT_WORKERS, EXIT_MODES, SYMBOLS, SimulationConfig, generate_all_candidates,
    load_csv_frames, simulate,
)
from config_microcapital import apply_agent_threshold_overrides, get_policy_config
from core.policy.envelope import canonical_policy_hash
from core.strategy import (
    DEFAULT_STRATEGY_ID, available_strategies, canonical_params, is_live_eligible, live_block_reason,
)

PROJECT_ROOT = Path(__file__).parents[1]
REGISTRY = PROJECT_ROOT / "artifacts" / "experiments" / "registry.jsonl"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "artifacts" / "replay" / "cache"
COST_STRESS_MULTIPLES = (1.0, 1.5, 2.0, 3.0)
R1_CACHE_KIND = "r1_bias_audit"
R1_MAX_DECISIONS = 60  # research.bias_audit defaults
R1_SEED = 7
# Code R1 runs besides the candidate code already in replay_cache.CANDIDATE_CODE_PATHS.
R1_CODE_PATHS = ("research/bias_audit.py", "research/common.py",
                 "core/agents/adversarial_agent.py", "core/agents/portfolio_manager.py")
NO_GATE_LINE = "# NOT EVIDENCE: authenticity gate skipped (--no-gate)"


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


def _strategy_params_arg(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"--strategy-params is not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("--strategy-params must be a JSON object")
    return value


def add_strategy_args(parser: argparse.ArgumentParser) -> None:
    """--strategy and --strategy-params, shared by run_replay, edge_diagnostics and the research CLIs."""
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY_ID, choices=available_strategies(),
                        help=f"registered strategy (default {DEFAULT_STRATEGY_ID}, the live strategy)")
    parser.add_argument("--strategy-params", type=_strategy_params_arg, default=None,
                        help="JSON object of strategy parameters (default: the values frozen in its hypothesis card)")


def resolve_strategy_args(parser: argparse.ArgumentParser, args) -> dict:
    """Canonical params for args.strategy; argparse error (exit 2) if they are invalid."""
    try:
        return canonical_params(args.strategy, args.strategy_params)
    except ValueError as exc:
        parser.error(str(exc))  # exits with status 2


def strategy_report_fields(strategy_id: str, params: dict) -> dict:
    fields = {"strategy_id": strategy_id, "strategy_params": params, "live_eligible": is_live_eligible(strategy_id)}
    if not fields["live_eligible"]:
        fields["live_block_reason"] = live_block_reason(strategy_id)
    return fields


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _section(trades, equity, start_balance, label_range, *, policy, frames, cost):
    # research imports run_replay.VARIANTS (research.common), so import it here, not at module level.
    from research.cost_stress import recost_trades
    from research.monte_carlo import risk_sweep

    m = trade_metrics(trades)
    by_exit = sorted(trades, key=lambda t: pd.Timestamp(t["exit_time"]))
    return {
        "range": label_range,
        "trades": m,
        "equity": equity_metrics(equity, start_balance) if not equity.empty else {},
        "by_symbol": breakdown(trades, "symbol"),
        "by_hour": breakdown(trades, "hour_utc"),
        "windows": windows(trades),
        "cost_stress": recost_trades(trades, frames, cost, COST_STRESS_MULTIPLES, scale_commission=True),
        "monte_carlo": risk_sweep([t["r_multiple"] for t in by_exit], policy=policy,
                                  exit_times=[t["exit_time"] for t in by_exit]) if trades else {},
    }


def _r1_bias_audit(frames, policy, policy_hash, dataset_hashes, symbols, start, end, cache_dir,
                   strategy_id=DEFAULT_STRATEGY_ID, strategy_params=None):
    """R1 depends only on the policy, the strategy, the data and the sample settings: compute once and cache."""
    from research import bias_audit

    params = canonical_params(strategy_id, strategy_params)
    parts = {"policy_hash": policy_hash, "datasets": dataset_hashes, "symbols": list(symbols),
             "start": start.isoformat(), "end": end.isoformat(), "max_decisions": R1_MAX_DECISIONS,
             "seed": R1_SEED, "r1_code": replay_cache.code_fingerprint(PROJECT_ROOT, R1_CODE_PATHS),
             "strategy_id": strategy_id, "strategy_params": params}
    key = replay_cache.cache_key(R1_CACHE_KIND, **parts)
    meta = replay_cache.key_parts(R1_CACHE_KIND, **parts)
    if cache_dir is not None:
        cached = replay_cache.load(cache_dir, R1_CACHE_KIND, key, meta)
        if cached is not None:
            return cached
    report = bias_audit.run_bias_audit(frames, policy, COST_SCENARIOS["base"], start=start, end=end,
                                       max_decisions=R1_MAX_DECISIONS, seed=R1_SEED,
                                       strategy_id=strategy_id, strategy_params=params)
    report = json.loads(json.dumps(report, default=str))  # same shape whether computed or cached
    if cache_dir is not None:
        replay_cache.store(cache_dir, R1_CACHE_KIND, key, report, meta)
    return report


def _display_path(path: Path) -> str:
    """Path relative to fx_ai_engine/ when the report is inside it (--out may point elsewhere)."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _skipped_gate(policy_hash: str, context: dict) -> dict:
    return {"tool": "r2_authenticity_gate", "verdict": "SKIPPED", "evidence_status": "NOT_EVIDENCE",
            "reasons": ["GATE_SKIPPED: run with --no-gate"], "warnings": [], "checks": [],
            "policy_hash": policy_hash, **context}


def _gate_line(gate: dict) -> str:
    from research.authenticity_gate import render_gate_markdown

    if gate["verdict"] == "SKIPPED":
        return NO_GATE_LINE
    return render_gate_markdown(gate).splitlines()[0]


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
    parser.add_argument("--no-gate", action="store_true",
                        help="skip the R1 bias audit and R2 authenticity gate; reports are labelled NOT EVIDENCE")
    add_strategy_args(parser)
    args = parser.parse_args(argv)
    strategy_id = args.strategy
    strategy_params = resolve_strategy_args(parser, args)
    if strategy_id != DEFAULT_STRATEGY_ID and list(args.variants) != ["baseline"]:
        parser.error(f"--strategy {strategy_id} accepts only --variants baseline "
                     "(variants are threshold overrides of the default rules)")
    strategy_fields = strategy_report_fields(strategy_id, strategy_params)
    research_only_line = ("" if strategy_fields["live_eligible"]
                          else f"Research only: {strategy_fields['live_block_reason']}\n")
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
                                                      cache_dir=cache_dir, strategy_id=strategy_id,
                                                      strategy_params=strategy_params)
        print(f"[{variant}] {len(candidates)} candidates", flush=True)
        r1_report = None
        if not args.no_gate:
            print(f"[{variant}] R1 bias audit ...", flush=True)
            r1_report = _r1_bias_audit(frames, policy, policy_hash, dataset_hashes, symbols, start, data_end, cache_dir,
                                       strategy_id, strategy_params)
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
                if strategy_id != DEFAULT_STRATEGY_ID:
                    label = f"{strategy_id}__{label}"
                gate_context = {"variant": label, "dataset_sha256_16": dataset_hashes, "strategy_id": strategy_id,
                                "strategy_params": strategy_params}
                if r1_report is None:
                    gate = _skipped_gate(policy_hash, {**gate_context, "exit_mode": exit_mode, "cost": cost.name})
                else:
                    from research.authenticity_gate import audit_replay

                    gate = audit_replay(frames=frames, policy=policy, cost=cost, candidates=candidates, result=result,
                                        exit_mode=exit_mode, r1_report=r1_report, context=gate_context)
                section_kw = {"policy": policy, "frames": frames, "cost": cost}
                report = {
                    "variant": label, "cost": cost_name, "generated_at": run_id, "policy_hash": policy_hash,
                    **strategy_fields,
                    "datasets": datasets, "dataset_sha256_16": dataset_hashes, "assumptions": ASSUMPTIONS,
                    "holdout_start": holdout_start.isoformat(),
                    "gate_verdict": gate["verdict"], "evidence_status": gate["evidence_status"],
                    "development": _section(dev, eq[eq.index < holdout_start], 100_000.0,
                                            f"{start.date()} to {holdout_start.date()}", **section_kw),
                    "holdout": _section(hold, eq[eq.index >= holdout_start], float(eq[eq.index < holdout_start]["balance"].iloc[-1]) if (eq.index < holdout_start).any() else 100_000.0,
                                        f"{holdout_start.date()} to {data_end.date()}", **section_kw),
                    "decisions": dict(result.decisions), "risk_events": result.risk_events[:200],
                    "candidate_funnel": {s: dict(f) for s, f in funnels.items()},
                    "overrides": VARIANTS[variant],
                }
                stem = out_root / f"{label}__{cost_name}"
                stem.with_name(stem.name + "_gate.json").write_text(json.dumps(gate, indent=2, default=str),
                                                                    encoding="utf-8")
                stem.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
                stem.with_suffix(".md").write_text(_gate_line(gate) + "\n" + research_only_line + "\n"
                                                   + render_markdown(report), encoding="utf-8")
                pd.DataFrame(result.trades).to_csv(stem.with_name(stem.name + "_trades.csv"), index=False)
                d = report["development"]["trades"]
                h = report["holdout"]["trades"]
                row = {"run_id": run_id, "variant": label, "cost": cost_name, "policy_hash": policy_hash,
                       **strategy_fields,
                       "datasets": dataset_hashes, "dev": {k: d.get(k) for k in ("trades", "win_rate", "expectancy_r", "profit_factor")},
                       "dev_max_dd": report["development"]["equity"].get("max_drawdown"),
                       "holdout": {k: h.get(k) for k in ("trades", "win_rate", "expectancy_r", "profit_factor")},
                       "gate_verdict": gate["verdict"], "evidence_status": gate["evidence_status"],
                       "report": _display_path(stem.with_suffix(".md"))}
                with REGISTRY.open("a", encoding="utf-8") as reg:
                    reg.write(json.dumps(row, default=str) + "\n")
                summary.append(row)
                print(f"  {label:34} {cost_name:12} dev n={d.get('trades')} E={d.get('expectancy_r')} PF={d.get('profit_factor')} "
                      f"DD={row['dev_max_dd']} | holdout n={h.get('trades')} E={h.get('expectancy_r')} "
                      f"| gate {gate['verdict']} ({gate['evidence_status']})", flush=True)

    (out_root / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"Reports: {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
