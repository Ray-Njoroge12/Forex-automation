"""Look-ahead and indicator warm-up checks for the replay (plan E4).

Usage (from fx_ai_engine/):
    python -m backtesting.bias_checks --data-dir artifacts/datasets/mt5_2026-11-03 --samples 400

1. Look-ahead: for sampled decision times, recompute the candidate with every
   bar after the decision removed. Any difference means the strategy saw the future.
2. Warm-up (recursive bias): the live engine computes EMA(200) on the last 350
   bars. Recompute each sampled decision with 2,000 bars of history and report
   how often the regime/technical outcome changes. Replay uses the same 350-bar
   window as live, so this measures sensitivity, not a live/replay mismatch.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import pandas as pd

from backtesting.unified_replay import (
    COST_SCENARIOS, SYMBOLS, TIMEFRAME_SECONDS, generate_candidates, load_csv_frames,
)
from config_microcapital import get_policy_config


def _outcome(cands):
    if not cands:
        return None
    c = cands[0]
    return (c.signal["direction"], round(c.signal["stop_pips"], 2), round(c.signal["take_profit_pips"], 2))


def _truncate(frames, symbol, t):
    out = {}
    for tf, df in frames[symbol].items():
        closes = df.index + pd.Timedelta(seconds=TIMEFRAME_SECONDS[tf])
        out[tf] = df[closes <= t]
    return {symbol: out}


def run(data_dir: Path, samples: int, seed: int = 3) -> dict:
    policy = get_policy_config(mode_id="core_srs", env={})
    cost = COST_SCENARIOS["base"]
    rng = random.Random(seed)
    report = {}
    for symbol in SYMBOLS:
        frames = load_csv_frames(data_dir, symbols=(symbol,))
        m15 = frames[symbol]["M15"]
        times = list(m15.index[3000:] + pd.Timedelta(minutes=15))
        picks = sorted(rng.sample(times, min(samples, len(times))))
        lookahead_diffs = warmup_diffs = signals = 0
        examples = []
        for t in picks:
            base = _outcome(generate_candidates(symbol, frames, policy, cost, decision_times=[t])[0])
            trunc = _outcome(generate_candidates(symbol, _truncate(frames, symbol, t), policy, cost, decision_times=[t])[0])
            long = _outcome(generate_candidates(symbol, frames, policy, cost, decision_times=[t], lookback_override=2000)[0])
            signals += base is not None
            if base != trunc:
                lookahead_diffs += 1
                examples.append({"time": t.isoformat(), "base": base, "truncated": trunc})
            if base != long:
                warmup_diffs += 1
        report[symbol] = {"samples": len(picks), "signals": signals, "lookahead_differences": lookahead_diffs,
                          "warmup_350_vs_2000_differences": warmup_diffs,
                          "warmup_difference_rate": round(warmup_diffs / max(len(picks), 1), 4),
                          "lookahead_examples": examples[:5]}
        print(symbol, report[symbol], flush=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay look-ahead and warm-up checks")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=400)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = run(args.data_dir, args.samples)
    out = args.out or args.data_dir / "bias_checks.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    failed = any(r["lookahead_differences"] for r in report.values())
    print("LOOK-AHEAD:", "FAIL" if failed else "PASS", f"-> {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
