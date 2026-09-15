"""Walk-forward optimization for the FX AI Engine backtester.

Slices a historical OHLC CSV into rolling train/test windows and reports
out-of-sample metrics. It does not tune parameters or infer an "edge" from a
single aggregate score.

Usage:
    python -m backtesting.walk_forward \\
        --csv tests/fixtures/ohlc_fixture.csv \\
        --symbol EURUSD \\
        --train 6 --test 1
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtesting.bt_runner import run_backtest_on_df
from backtesting.data_loader import load_ohlc_csv
from backtesting.simulation_profile import build_simulation_profile
from config_microcapital import MODE_CONFIGS

# SRS pre-live validation thresholds (from SRS v1).
SRS_MIN_WIN_RATE = 0.45
SRS_MIN_AVG_R = 2.0
SRS_MAX_DRAWDOWN_PCT = 15.0
MIN_TRAIN_BARS = 250  # EMA-200 plus a small warmup margin.
MIN_TEST_BARS = 50


def _run_on_df(df: pd.DataFrame, symbol: str, *, mode_id: str | None = None):
    """Run a single backtest pass on a pre-sliced DataFrame."""
    return run_backtest_on_df(df, symbol, mode_id=mode_id)


def _extract_metrics(strategy: AgentBacktestStrategy) -> dict:
    """Summarise closed-trade results and analyser data into a metrics dict."""
    closed = strategy.results
    total = len(closed)
    profile = getattr(strategy, "simulation_profile", build_simulation_profile("core_srs"))

    sharpe_analysis = strategy.analyzers.sharpe.get_analysis()
    drawdown_analysis = strategy.analyzers.drawdown.get_analysis()
    sharpe_val = float(sharpe_analysis.get("sharperatio") or 0.0)
    max_dd = float(
        getattr(strategy, "max_simulated_drawdown_pct", 0.0)
        if profile.realistic_constraints
        else drawdown_analysis.get("max", {}).get("drawdown") or 0.0
    )

    if total == 0:
        return {
            "total": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "sharpe": sharpe_val,
            "max_dd": max_dd,
            "rejections": len(getattr(strategy, "rejected_signals", [])),
            "profile": profile,
        }

    wins = sum(1 for t in closed if t["pnl"] > 0)
    return {
        "total": total,
        "win_rate": wins / total,
        "avg_r": sum(t["r_multiple"] for t in closed) / total,
        "sharpe": sharpe_val,
        "max_dd": max_dd,
        "rejections": len(getattr(strategy, "rejected_signals", [])),
        "profile": profile,
    }


def _check_srs(metrics: dict) -> bool:
    """Return True if the test window satisfies SRS pre-live validation criteria."""
    return (
        metrics["win_rate"] >= SRS_MIN_WIN_RATE
        and metrics["avg_r"] >= SRS_MIN_AVG_R
        and metrics["max_dd"] <= SRS_MAX_DRAWDOWN_PCT
    )


def run_walk_forward(
    csv_path: str,
    symbol: str,
    train_months: int = 6,
    test_months: int = 1,
    mode_id: str | None = None,
) -> pd.DataFrame:
    """Run walk-forward validation on a historical CSV.

    Rolls a window of ``train_months`` followed by ``test_months`` across the
    full dataset, advancing by ``test_months`` on each iteration. Returns one
    row per window plus transparent train/test gaps and an out-of-sample pass
    rate; it does not run a parameter search.
    """
    df = load_ohlc_csv(csv_path)
    if df.empty:
        return pd.DataFrame()

    profile = build_simulation_profile(mode_id)
    windows: list[dict] = []
    window_start = df.index[0]
    dataset_end = df.index[-1]

    while True:
        train_end = window_start + pd.DateOffset(months=train_months)
        test_end = train_end + pd.DateOffset(months=test_months)

        if test_end > dataset_end:
            break

        # Slice — use boolean indexing to keep boundaries clean.
        train_df = df[(df.index >= window_start) & (df.index < train_end)]
        test_df = df[(df.index >= train_end) & (df.index < test_end)]

        # The strategy needs at least EMA-200 plus warmup; short windows make
        # the apparent performance an artifact of indicator initialization.
        if len(train_df) < MIN_TRAIN_BARS or len(test_df) < MIN_TEST_BARS:
            window_start += pd.DateOffset(months=test_months)
            continue

        train_m = _extract_metrics(_run_on_df(train_df, symbol, mode_id=mode_id))
        test_m = _extract_metrics(_run_on_df(test_df, symbol, mode_id=mode_id))

        windows.append(
            {
                "window": str(window_start.date()),
                "simulation_mode": profile.mode_id,
                "evidence_label": profile.evidence_label,
                "evidence_stream": profile.evidence_stream,
                "realism_label": profile.realism_label,
                "criteria_label": (
                    "SRS benchmark only"
                    if profile.realistic_constraints
                    else "Core SRS smoke-test criteria"
                ),
                "starting_cash": profile.starting_cash,
                "train_trades": train_m["total"],
                "test_trades": test_m["total"],
                "train_rejections": train_m["rejections"],
                "test_rejections": test_m["rejections"],
                "train_wr": round(train_m["win_rate"], 4),
                "test_wr": round(test_m["win_rate"], 4),
                "train_avg_r": round(train_m["avg_r"], 4),
                "test_avg_r": round(test_m["avg_r"], 4),
                "train_sharpe": round(train_m["sharpe"], 4),
                "test_sharpe": round(test_m["sharpe"], 4),
                "train_max_dd": round(train_m["max_dd"], 2),
                "test_max_dd": round(test_m["max_dd"], 2),
                "srs_criteria_met": _check_srs(test_m),
            }
        )

        window_start += pd.DateOffset(months=test_months)

    if not windows:
        return pd.DataFrame()

    results = pd.DataFrame(windows)

    # Report observed train-to-test gaps rather than mislabelling correlation
    # of win rates as parameter stability (no parameter search occurs here).
    results["win_rate_gap"] = (results["train_wr"] - results["test_wr"]).abs().round(4)
    results["avg_r_gap"] = (results["train_avg_r"] - results["test_avg_r"]).abs().round(4)
    results["test_window_pass_rate"] = round(float(results["srs_criteria_met"].mean()), 4)

    return results


def _print_walk_forward_report(results: pd.DataFrame) -> None:
    if results.empty:
        print("No walk-forward windows could be formed (insufficient data).")
        return

    first = results.iloc[0]
    print("=" * 64)
    print("Walk-Forward Results")
    print("=" * 64)
    print(f"Evidence: {first['evidence_label']} [{first['evidence_stream']}]")
    print(f"Simulation: {first['realism_label']} | starting_cash=${first['starting_cash']:.2f}")
    print(f"Criteria: {first['criteria_label']}")
    for _, row in results.iterrows():
        srs_flag = "PASS" if row["srs_criteria_met"] else "FAIL"
        simulation_mode = row.get("simulation_mode")
        if pd.isna(simulation_mode):
            simulation_mode = (
                "preserve_10"
                if row.get("evidence_stream") == "preserve_10_realistic"
                else "core_srs"
            )
        criteria_token = "SRS_BENCHMARK" if simulation_mode == "preserve_10" else "SRS"
        print(
            f"  {row['window']}  "
            f"train_wr={row['train_wr']:.2%}  test_wr={row['test_wr']:.2%}  "
            f"test_avg_r={row['test_avg_r']:.2f}  test_dd={row['test_max_dd']:.1f}%  "
            f"rejects={int(row['test_rejections'])}  {criteria_token}={srs_flag}"
        )
    print("-" * 64)
    print(f"Out-of-sample pass rate: {results['test_window_pass_rate'].iloc[0]:.1%}")
    print(f"Mean absolute win-rate gap: {results['win_rate_gap'].mean():.1%}")
    print(f"Mean absolute avg-R gap: {results['avg_r_gap'].mean():.3f}")
    print("=" * 64)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward optimisation for FX AI Engine")
    parser.add_argument("--csv", required=True, help="Path to OHLC CSV file")
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--train", type=int, default=6, help="Training window in months")
    parser.add_argument("--test", type=int, default=1, help="Test window in months")
    parser.add_argument("--policy-mode", choices=sorted(MODE_CONFIGS), default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not Path(args.csv).exists():
        raise SystemExit(f"CSV not found: {args.csv}")

    results = run_walk_forward(args.csv, args.symbol, args.train, args.test, mode_id=args.policy_mode)
    _print_walk_forward_report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
