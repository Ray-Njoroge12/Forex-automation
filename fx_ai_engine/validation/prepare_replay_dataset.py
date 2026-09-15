"""Validate an OHLC CSV and write the manifest required before replay runs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backtesting.dataset_manifest import inspect_ohlc_csv


def write_manifest(*, csv_path: Path, symbol: str, output: Path) -> Path:
    manifest = inspect_ohlc_csv(csv_path, symbol=symbol)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate historical FX OHLC data for replay")
    parser.add_argument("--csv", required=True, type=Path, help="OHLC CSV with time,open,high,low,close[,volume]")
    parser.add_argument("--symbol", required=True, help="Six-letter FX pair, for example EURUSD")
    parser.add_argument("--output", required=True, type=Path, help="Manifest JSON output path")
    args = parser.parse_args(argv)
    target = write_manifest(csv_path=args.csv, symbol=args.symbol, output=args.output)
    print(f"Validated replay dataset manifest: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
