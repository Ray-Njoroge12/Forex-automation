"""Deterministic synthetic registry and replay outputs for the R3 multiple-testing check.

Six configurations (baseline, coded_defaults, pullback_2 x policy, fixed_tp, all ``__edge``), one run.
Each trade is +2.2R or -1R. Noise configurations have expectancy 0; ``edge_label`` has ``edge_mean_r``.
The files follow run_replay's layout and schema. This is test data, never evidence.

Usage (from fx_ai_engine/):
    USE_MT5_MOCK=1 ../.venv/bin/python -m research.multiple_testing_fixture --out /tmp/r3-synthetic
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

VARIANTS = ("baseline", "coded_defaults", "pullback_2")
EXIT_MODES = ("policy", "fixed_tp")
HALTS = "edge"
COST = "base"
RUN_ID = "20260701T000000Z"
REWARD_R = 2.2
DEV_START, HOLDOUT_START, HOLDOUT_END = "2023-01-02", "2026-01-01", "2026-06-30"
REPLAY_DIR = Path("artifacts") / "replay" / "synthetic"


def _labels() -> list[tuple[str, str]]:
    return [(v, f"{v}__{e}__{HALTS}") for v in VARIANTS for e in EXIT_MODES]


def _times(rng: np.random.Generator, start: str, end: str, n: int) -> pd.DatetimeIndex:
    """n distinct weekday M15 times in [start, end), 01:00 to 20:45 UTC, sorted."""
    grid = pd.date_range(start, end, freq="15min", tz="UTC", inclusive="left")
    grid = grid[(grid.dayofweek < 5) & (grid.hour >= 1) & (grid.hour <= 20)]
    return grid[np.sort(rng.choice(len(grid), size=n, replace=False))]


def _metrics(r: Sequence[float]) -> dict[str, Any]:
    arr = np.asarray(r, dtype=float)
    if not arr.size:
        return {"trades": 0, "win_rate": None, "expectancy_r": None, "profit_factor": None}
    wins, losses = arr[arr > 0].sum(), -arr[arr < 0].sum()
    return {"trades": int(arr.size), "win_rate": round(float((arr > 0).mean()), 4),
            "expectancy_r": round(float(arr.mean()), 4),
            "profit_factor": round(float(wins / losses), 4) if losses > 0 else None}


def _max_dd_r(r: Sequence[float]) -> float:
    eq = np.cumsum(np.asarray(r, dtype=float))
    return round(float(np.max(np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] - eq, initial=0.0)), 4)


def write_synthetic_fixture(root: Path, *, seed: int = 7, trades_per_config: int = 600,
                            edge_label: str = "pullback_2__fixed_tp__edge", edge_mean_r: float = 0.6) -> Path:
    """Write registry.jsonl and per-configuration replay files under ``root``; return the registry path.

    ``trades_per_config`` is the dev trade count; the holdout gets a sixth of it.
    """
    root = Path(root)
    replay_dir = root / REPLAY_DIR
    replay_dir.mkdir(parents=True, exist_ok=True)
    registry = root / "artifacts" / "experiments" / "registry.jsonl"
    registry.parent.mkdir(parents=True, exist_ok=True)
    n_hold = max(1, trades_per_config // 6)
    rows = []
    for idx, (variant, label) in enumerate(_labels()):
        rng = np.random.default_rng([seed, idx])
        p_win = ((edge_mean_r if label == edge_label else 0.0) + 1.0) / (REWARD_R + 1.0)
        dev_t = _times(rng, DEV_START, HOLDOUT_START, trades_per_config)
        hold_t = _times(rng, HOLDOUT_START, HOLDOUT_END, n_hold)
        times = dev_t.append(hold_t)
        r = np.where(rng.random(len(times)) < p_win, REWARD_R, -1.0)
        policy_hash = hashlib.sha256(variant.encode("utf-8")).hexdigest()
        stem = replay_dir / f"{label}__{COST}"
        trades = pd.DataFrame({
            "trade_id": [f"SYN_{i + 1:06d}_EURUSD" for i in range(len(times))],
            "symbol": "EURUSD", "direction": np.where(rng.random(len(times)) < 0.5, "BUY", "SELL"),
            "entry_time": [t.isoformat() for t in times],
            "exit_time": [(t + pd.Timedelta(hours=2)).isoformat() for t in times],
            "r_multiple": r, "stop_pips": 10.0})
        trades.to_csv(stem.with_name(stem.name + "_trades.csv"), index=False)
        dev_r, hold_r = r[:trades_per_config], r[trades_per_config:]
        report = {"variant": label, "cost": COST, "generated_at": RUN_ID, "policy_hash": policy_hash,
                  "holdout_start": pd.Timestamp(HOLDOUT_START, tz="UTC").isoformat(),
                  "development": {"range": f"{DEV_START} to {HOLDOUT_START}", **_metrics(dev_r)},
                  "holdout": {"range": f"{HOLDOUT_START} to {HOLDOUT_END}", **_metrics(hold_r)}}
        stem.with_suffix(".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        stem.with_suffix(".md").write_text(f"# Synthetic replay: {label} / {COST}\n\nTest fixture for R3; not evidence.\n",
                                           encoding="utf-8")
        rows.append({"run_id": RUN_ID, "variant": label, "cost": COST, "policy_hash": policy_hash,
                     "datasets": {"synthetic": f"seed-{seed}"}, "dev": _metrics(dev_r), "dev_max_dd": _max_dd_r(dev_r),
                     "holdout": _metrics(hold_r), "report": stem.with_suffix(".md").relative_to(root).as_posix()})
    registry.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the synthetic R3 fixture (registry plus replay files)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    path = write_synthetic_fixture(args.out, seed=args.seed)
    print(f"Synthetic registry: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
