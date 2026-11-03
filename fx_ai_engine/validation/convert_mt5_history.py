"""Convert FX_HistoryExport CSVs (raw MT5 server time) into UTC replay CSVs.

MT5 bar times are broker server time. Many FX servers, including
MetaQuotes-Demo, follow the "New York + 7h" convention: server time is
GMT+2 while the US is on standard time and GMT+3 during US daylight time.
A single fixed offset would shift half of a multi-year history by an hour.

Usage (from fx_ai_engine/):
    python -m validation.convert_mt5_history \
        --input "<MQL5/Files>/history/EURUSD_M15.csv" \
        --output artifacts/datasets/EURUSD_M15.csv

The export's meta file records the offset observed at export time; the
conversion refuses to run when that offset contradicts the chosen convention.
Then build the manifest with validation.prepare_replay_dataset.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

NEW_YORK = ZoneInfo("America/New_York")
CONVENTIONS = ("ny_plus_7", "fixed")


def ny_plus_7_offset_seconds(server_epoch: int) -> int:
    """Server-minus-UTC offset under the New York + 7h convention."""
    naive = datetime(1970, 1, 1) + timedelta(seconds=int(server_epoch))
    ny_local = naive - timedelta(hours=7)
    utc = ny_local.replace(tzinfo=NEW_YORK).astimezone(timezone.utc).replace(tzinfo=None)
    return int((naive - utc).total_seconds())


def server_to_utc(server_epochs: pd.Series, *, convention: str, fixed_offset_seconds: int = 0) -> pd.Series:
    if convention == "ny_plus_7":
        offsets = server_epochs.map(ny_plus_7_offset_seconds)
    elif convention == "fixed":
        offsets = pd.Series(fixed_offset_seconds, index=server_epochs.index)
    else:
        raise ValueError(f"unknown convention {convention!r}; expected one of {CONVENTIONS}")
    return pd.to_datetime(server_epochs - offsets, unit="s", utc=True)


def convert(input_csv: Path, output_csv: Path, *, convention: str = "ny_plus_7", fixed_offset_seconds: int = 0) -> dict:
    frame = pd.read_csv(input_csv)
    expected = ["server_time", "open", "high", "low", "close", "tick_volume", "spread_points"]
    if list(frame.columns) != expected:
        raise ValueError(f"unexpected columns {list(frame.columns)}; expected {expected}")
    if frame.empty:
        raise ValueError("history export is empty")

    meta_path = input_csv.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    observed = meta.get("export_server_utc_offset_seconds")
    if convention == "ny_plus_7" and observed is not None:
        implied = ny_plus_7_offset_seconds(int(frame["server_time"].iloc[-1]))
        if int(observed) != implied:
            raise ValueError(
                f"export offset {observed}s contradicts ny_plus_7 ({implied}s at the last bar); "
                "use --convention fixed with a verified offset"
            )

    times = server_to_utc(frame["server_time"].astype("int64"), convention=convention, fixed_offset_seconds=fixed_offset_seconds)
    out = pd.DataFrame({
        "time": times.dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": frame["open"], "high": frame["high"], "low": frame["low"], "close": frame["close"],
        "volume": frame["tick_volume"], "spread_points": frame["spread_points"],
    })
    if out["time"].duplicated().any():
        raise ValueError("UTC conversion produced duplicate timestamps; check the server-time convention")
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_csv, index=False)
    return {
        "input": str(input_csv), "output": str(output_csv), "rows": len(out),
        "first_utc": out["time"].iloc[0], "last_utc": out["time"].iloc[-1],
        "convention": convention, "export_offset_seconds": observed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert MT5 server-time history exports to UTC")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--convention", choices=CONVENTIONS, default="ny_plus_7")
    parser.add_argument("--fixed-offset-seconds", type=int, default=0)
    args = parser.parse_args(argv)
    print(json.dumps(convert(args.input, args.output, convention=args.convention,
                             fixed_offset_seconds=args.fixed_offset_seconds), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
