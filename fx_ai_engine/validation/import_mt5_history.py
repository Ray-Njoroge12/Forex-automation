"""Convert every FX_HistoryExport file to UTC and write hash manifests (plan E1/E2).

Usage (from fx_ai_engine/):
    python -m validation.import_mt5_history
    python -m validation.import_mt5_history --source "<MQL5/Files>/history" --out artifacts/datasets/mt5_2026-11-03

Output: one ``SYMBOL_TF.csv`` per export (UTC, columns time,open,high,low,close,volume,spread_points),
``SYMBOL_TF.manifest.json`` per file, and ``coverage.json`` summarising rows, range and gaps.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtesting.dataset_manifest import inspect_ohlc_csv
from core.env_loader import load_runtime_env
from validation.convert_mt5_history import convert

PROJECT_ROOT = Path(__file__).parents[1]
TF_SECONDS = {"M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}


def _gap_report(csv_path: Path, tf: str) -> dict:
    times = pd.to_datetime(pd.read_csv(csv_path, usecols=["time"])["time"], utc=True)
    deltas = times.diff().dt.total_seconds().dropna()
    step = TF_SECONDS[tf]
    # Weekend gaps (~2 days) are expected; flag gaps over 3 days on weekdays as suspicious.
    weekday_gaps = deltas[(deltas > step * 1.5) & (deltas < 2 * 86400)]
    long_gaps = deltas[deltas > 3.5 * 86400]
    return {"rows": len(times), "first": times.iloc[0].isoformat(), "last": times.iloc[-1].isoformat(),
            "years": round((times.iloc[-1] - times.iloc[0]).days / 365.25, 2),
            "intraweek_gaps": int(weekday_gaps.size), "gaps_over_3_5_days": int(long_gaps.size)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import MT5 history exports as UTC replay datasets")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    load_runtime_env()
    source = args.source
    if source is None:
        bridge = os.getenv("BRIDGE_BASE_PATH")
        if not bridge:
            raise SystemExit("pass --source or set BRIDGE_BASE_PATH")
        source = Path(bridge).parent / "history"
    out = args.out or PROJECT_ROOT / "artifacts" / "datasets" / f"mt5_{datetime.now(timezone.utc):%Y-%m-%d}"
    out.mkdir(parents=True, exist_ok=True)
    exports = sorted(source.glob("*_*.csv"))
    if not exports:
        raise SystemExit(f"no exports in {source}; run FX_HistoryExport in MT5 first")
    coverage = {}
    for path in exports:
        symbol, tf = path.stem.split("_", 1)
        target = out / f"{symbol}_{tf}.csv"
        convert(path, target)
        manifest = inspect_ohlc_csv(target, symbol=symbol)
        (out / f"{symbol}_{tf}.manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
        coverage[f"{symbol}_{tf}"] = {**_gap_report(target, tf), "sha256": manifest.sha256}
        print(f"{symbol}_{tf}: {coverage[f'{symbol}_{tf}']['rows']} rows "
              f"{coverage[f'{symbol}_{tf}']['first'][:10]} .. {coverage[f'{symbol}_{tf}']['last'][:10]}")
    (out / "coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    print(f"Datasets written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
