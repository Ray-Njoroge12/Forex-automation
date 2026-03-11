from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtesting.walk_forward import run_walk_forward
from core.mt5_bridge import PRESERVE_10_REQUIRED_SYMBOLS
from config_microcapital import MODE_CONFIGS


def _summarize_symbol(symbol: str, results: pd.DataFrame) -> dict[str, object]:
    if results.empty:
        return {
            "symbol": symbol,
            "status": "NO_WINDOWS",
            "windows": 0,
            "test_trades": 0,
            "avg_test_wr": 0.0,
            "avg_test_avg_r": 0.0,
            "worst_test_dd": 0.0,
            "pass_windows": 0,
            "stability": float("nan"),
        }
    return {
        "symbol": symbol,
        "status": "OK",
        "windows": int(len(results)),
        "test_trades": int(results["test_trades"].sum()),
        "avg_test_wr": float(results["test_wr"].mean()),
        "avg_test_avg_r": float(results["test_avg_r"].mean()),
        "worst_test_dd": float(results["test_max_dd"].max()),
        "pass_windows": int(results["srs_criteria_met"].sum()),
        "stability": float(results["param_stability_score"].iloc[0]),
    }


def run_walk_forward_suite(
    data_dir: str,
    *,
    symbols: tuple[str, ...] = PRESERVE_10_REQUIRED_SYMBOLS,
    train_months: int = 6,
    test_months: int = 1,
    mode_id: str | None = None,
) -> pd.DataFrame:
    base_dir = Path(data_dir)
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        csv_path = base_dir / f"{symbol}.csv"
        if not csv_path.exists():
            rows.append(
                {
                    "symbol": symbol,
                    "status": "MISSING_DATA",
                    "windows": 0,
                    "test_trades": 0,
                    "avg_test_wr": 0.0,
                    "avg_test_avg_r": 0.0,
                    "worst_test_dd": 0.0,
                    "pass_windows": 0,
                    "stability": float("nan"),
                    "csv_path": str(csv_path),
                }
            )
            continue

        results = run_walk_forward(
            str(csv_path),
            symbol,
            train_months=train_months,
            test_months=test_months,
            mode_id=mode_id,
        )
        row = _summarize_symbol(symbol, results)
        row["csv_path"] = str(csv_path)
        rows.append(row)
    return pd.DataFrame(rows)


def _print_suite_report(summary: pd.DataFrame) -> None:
    if summary.empty:
        print("No symbols processed.")
        return

    print("=" * 88)
    print("Walk-Forward Suite Summary")
    print("=" * 88)
    print(
        f"{'symbol':<8} {'status':<12} {'windows':>7} {'test_trades':>11} "
        f"{'avg_test_wr':>11} {'avg_test_avg_r':>15} {'worst_dd':>10} {'pass_windows':>13} {'stability':>10}"
    )
    for _, row in summary.iterrows():
        stability = row["stability"]
        stability_text = "nan" if pd.isna(stability) else f"{float(stability):.3f}"
        print(
            f"{row['symbol']:<8} {row['status']:<12} {int(row['windows']):>7} {int(row['test_trades']):>11} "
            f"{float(row['avg_test_wr']):>11.2%} {float(row['avg_test_avg_r']):>15.3f} "
            f"{float(row['worst_test_dd']):>10.2f} {int(row['pass_windows']):>13} {stability_text:>10}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run walk-forward testing across the full six-pair universe")
    parser.add_argument("--data-dir", required=True, help="Directory containing <SYMBOL>.csv OHLC files")
    parser.add_argument("--symbol", action="append", dest="symbols", help="Optional symbol override; may be repeated")
    parser.add_argument("--train", type=int, default=6, help="Training window in months")
    parser.add_argument("--test", type=int, default=1, help="Test window in months")
    parser.add_argument("--policy-mode", choices=sorted(MODE_CONFIGS), default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    symbols = tuple(args.symbols) if args.symbols else PRESERVE_10_REQUIRED_SYMBOLS
    summary = run_walk_forward_suite(
        args.data_dir,
        symbols=symbols,
        train_months=args.train,
        test_months=args.test,
        mode_id=args.policy_mode,
    )
    _print_suite_report(summary)

    missing = int((summary["status"] == "MISSING_DATA").sum()) if not summary.empty else 0
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
