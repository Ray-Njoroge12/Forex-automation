"""Swap allowance for replay trades (stage D evaluation plan, step 5).

The replay models no swap (``run_replay.ASSUMPTIONS``). For each trade this
counts the 17:00 America/New_York rollovers in (entry, exit] on Monday to
Friday, with Wednesday counting 3 (the weekend), and charges only negative
swap:

    R -= rollovers * max(0, -swap_side) * point / pip / stop_pips

``swap_side`` is ``swap_long`` for BUY and ``swap_short`` for SELL, in points,
from an EA ``market/symbols.json`` snapshot (its sha256 is recorded). The EA
does not export ``swap_mode``; if "points" cannot be confirmed, use
``--flat-pips-per-rollover 1.0`` instead of the snapshot. Positive swap is
never credited.

Usage (from fx_ai_engine/):
    python -m research.swap_allowance --trades <run>_trades.csv --symbols-json <snapshot> \
        --holdout-start <report holdout_start> [--out swap_allowance.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NEW_YORK = ZoneInfo("America/New_York")
ROLLOVER_TIME = time(17, 0)
WEDNESDAY = 2


def pip_size(symbol: str) -> float:
    return 0.01 if "JPY" in symbol else 0.0001


def default_point(symbol: str) -> float:
    return 0.001 if "JPY" in symbol else 0.00001


def count_rollovers(entry: pd.Timestamp | datetime, exit: pd.Timestamp | datetime) -> int:
    """17:00 New York rollovers in (entry, exit], Monday-Friday, Wednesday counted 3 times."""
    start = pd.Timestamp(entry).tz_convert(NEW_YORK)
    end = pd.Timestamp(exit).tz_convert(NEW_YORK)
    if end <= start:
        return 0
    total = 0
    day: date = start.date()
    while day <= end.date():
        if day.weekday() < 5:
            instant = pd.Timestamp(datetime.combine(day, ROLLOVER_TIME), tz=NEW_YORK)
            if start < instant <= end:
                total += 3 if day.weekday() == WEDNESDAY else 1
        day += timedelta(days=1)
    return total


def load_swap_snapshot(path: Path) -> tuple[dict[str, dict[str, float]], str]:
    raw = Path(path).read_bytes()
    payload = json.loads(raw)
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, dict) or not symbols:
        raise ValueError(f"{path}: no 'symbols' object (expected an EA market/symbols.json snapshot)")
    out = {}
    for symbol, spec in symbols.items():
        if not isinstance(spec, dict):
            continue
        values = {k: spec.get(k) for k in ("swap_long", "swap_short")}
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in values.values()):
            raise ValueError(f"{path}: {symbol} has no finite swap_long/swap_short")
        point = spec.get("point")
        values["point"] = float(point) if isinstance(point, (int, float)) and point > 0 else default_point(symbol)
        out[str(symbol)] = {k: float(v) for k, v in values.items()}
    return out, hashlib.sha256(raw).hexdigest()[:16]


def swap_charge_r(trade: Mapping[str, Any], rollovers: int, *, swaps: Mapping[str, Mapping[str, float]] | None,
                  flat_pips: float | None = None) -> float:
    """R charged for ``rollovers`` nights (>= 0; positive swap is never credited)."""
    stop = float(trade["stop_pips"])
    if rollovers <= 0 or stop <= 0:
        return 0.0
    symbol = str(trade["symbol"])
    if flat_pips is not None:
        pips_per_night = float(flat_pips)
    else:
        if swaps is None or symbol not in swaps:
            raise ValueError(f"no swap rates for {symbol} in the snapshot")
        spec = swaps[symbol]
        side = spec["swap_long"] if str(trade["direction"]) == "BUY" else spec["swap_short"]
        pips_per_night = max(0.0, -side) * spec["point"] / pip_size(symbol)
    return rollovers * pips_per_night / stop


def apply_swap(trades: Sequence[Mapping[str, Any]], *, swaps: Mapping[str, Mapping[str, float]] | None = None,
               flat_pips: float | None = None) -> list[dict[str, Any]]:
    out = []
    for t in trades:
        rollovers = count_rollovers(pd.Timestamp(t["entry_time"]), pd.Timestamp(t["exit_time"]))
        charge = swap_charge_r(t, rollovers, swaps=swaps, flat_pips=flat_pips)
        out.append({**t, "rollovers": rollovers, "swap_r": round(charge, 6),
                    "r_multiple_swap_adjusted": float(t["r_multiple"]) - charge})
    return out


def section_metrics(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    raw = np.array([float(t["r_multiple"]) for t in trades])
    adj = np.array([float(t["r_multiple_swap_adjusted"]) for t in trades])
    hold = np.array([(pd.Timestamp(t["exit_time"]) - pd.Timestamp(t["entry_time"])).total_seconds() / 3600
                     for t in trades])
    wins, losses = adj[adj > 0], adj[adj < 0]
    return {
        "trades": int(adj.size),
        "expectancy_r": round(float(raw.mean()), 4),
        "expectancy_r_swap_adjusted": round(float(adj.mean()), 4),
        "std_r_swap_adjusted": round(float(adj.std(ddof=1)), 4) if adj.size > 1 else 0.0,
        "profit_factor_r_swap_adjusted": round(float(wins.sum() / -losses.sum()), 4) if losses.size else math.inf,
        "avg_winner_r_swap_adjusted": round(float(wins.mean()), 4) if wins.size else 0.0,
        "total_swap_r": round(float((raw - adj).sum()), 4),
        "mean_rollovers": round(float(np.mean([t["rollovers"] for t in trades])), 4),
        "median_holding_hours": round(float(np.median(hold)), 2),
    }


def swap_report(trades: Sequence[Mapping[str, Any]], holdout_start: pd.Timestamp, *,
                swaps: Mapping[str, Mapping[str, float]] | None = None, flat_pips: float | None = None,
                snapshot_sha256_16: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if (swaps is None) == (flat_pips is None):
        raise ValueError("exactly one of a swap snapshot or flat_pips is required")
    adjusted = apply_swap(trades, swaps=swaps, flat_pips=flat_pips)
    start = pd.Timestamp(holdout_start)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    dev = [t for t in adjusted if pd.Timestamp(t["entry_time"]) < start]
    hold = [t for t in adjusted if pd.Timestamp(t["entry_time"]) >= start]
    report = {
        "tool": "swap_allowance", "holdout_start": start.isoformat(),
        "method": "flat_pips_per_rollover" if flat_pips is not None else "snapshot_points",
        "flat_pips_per_rollover": flat_pips, "symbols_json_sha256_16": snapshot_sha256_16,
        "rule": "17:00 America/New_York rollovers in (entry, exit], Mon-Fri, Wednesday x3; negative swap only",
        "development": section_metrics(dev), "holdout": section_metrics(hold),
    }
    return report, adjusted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Swap allowance for replay trades (read-only)")
    parser.add_argument("--trades", type=Path, required=True, help="run_replay <stem>_trades.csv")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--symbols-json", type=Path, help="EA market/symbols.json snapshot (swap in points)")
    source.add_argument("--flat-pips-per-rollover", type=float, help="charge this many pips per rollover instead")
    parser.add_argument("--holdout-start", required=True, help="the replay report's holdout_start")
    parser.add_argument("--out", type=Path, default=None, help="report JSON (default: next to --trades)")
    args = parser.parse_args(argv)
    if args.flat_pips_per_rollover is not None and not (math.isfinite(args.flat_pips_per_rollover)
                                                        and args.flat_pips_per_rollover >= 0):
        parser.error("--flat-pips-per-rollover must be finite and >= 0")
    try:
        holdout = pd.Timestamp(args.holdout_start)
    except ValueError as exc:
        parser.error(f"--holdout-start: {exc}")
    trades = pd.read_csv(args.trades).to_dict("records")
    swaps, sha = (None, None)
    if args.symbols_json is not None:
        swaps, sha = load_swap_snapshot(args.symbols_json)
    report, adjusted = swap_report(trades, holdout, swaps=swaps, flat_pips=args.flat_pips_per_rollover,
                                   snapshot_sha256_16=sha)
    report["trades_csv"] = str(args.trades)
    out = args.out or args.trades.with_name(args.trades.stem + "_swap.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    pd.DataFrame(adjusted).to_csv(out.with_suffix(".csv"), index=False)
    dev = report["development"]
    print(f"development: n={dev.get('trades')} E={dev.get('expectancy_r')} "
          f"E_swap={dev.get('expectancy_r_swap_adjusted')} swap_R={dev.get('total_swap_r')}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
