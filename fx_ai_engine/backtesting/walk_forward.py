"""Walk-forward optimization for the FX AI Engine backtester.

Slices a historical OHLC CSV into rolling train/test windows, runs a full
backtest on each slice, and reports per-window performance metrics alongside
a param_stability_score (Pearson correlation of train_wr vs test_wr across
all windows).  A stability score > 0.6 suggests the strategy edge is real
and not overfit to the training period.

Usage:
    python -m backtesting.walk_forward \\
        --csv tests/fixtures/ohlc_fixture.csv \\
        --symbol EURUSD \\
        --train 6 --test 1
"""
from __future__ import annotations

import argparse
from pathlib import Path

import backtrader as bt
import pandas as pd

from backtesting.bt_strategy import AgentBacktestStrategy
from backtesting.data_loader import load_ohlc_csv

STARTING_CASH = 10_000.0

# SRS pre-live validation thresholds (from SRS v1).
SRS_MIN_WIN_RATE = 0.45
SRS_MIN_AVG_R = 2.0
SRS_MAX_DRAWDOWN_PCT = 15.0


def _run_on_df(df: pd.DataFrame, symbol: str) -> AgentBacktestStrategy:
    """Run a single backtest pass on a pre-sliced DataFrame."""
    data = bt.feeds.PandasData(
        dataname=df,
        open="open",
        high="high",
        low="low",
        close="close",
        volume="volume",
        datetime=None,
    )
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(STARTING_CASH)
    cerebro.adddata(data, name=symbol)
    cerebro.addstrategy(AgentBacktestStrategy, symbol=symbol)
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.0, annualize=True)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    results = cerebro.run()
    return results[0]


def _extract_metrics(strategy: AgentBacktestStrategy) -> dict:
    """Summarise closed-trade results and analyser data into a metrics dict."""
    closed = strategy.results
    total = len(closed)

    sharpe_analysis = strategy.analyzers.sharpe.get_analysis()
    drawdown_analysis = strategy.analyzers.drawdown.get_analysis()
    sharpe_val = float(sharpe_analysis.get("sharperatio") or 0.0)
    max_dd = float(drawdown_analysis.get("max", {}).get("drawdown") or 0.0)

    if total == 0:
        return {
            "total": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "sharpe": sharpe_val,
            "max_dd": max_dd,
        }

    wins = sum(1 for t in closed if t["pnl"] > 0)
    return {
        "total": total,
        "win_rate": wins / total,
        "avg_r": sum(t["r_multiple"] for t in closed) / total,
        "sharpe": sharpe_val,
        "max_dd": max_dd,
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
) -> pd.DataFrame:
    """Run walk-forward optimisation on a historical CSV.

    Rolls a window of ``train_months`` followed by ``test_months`` across the
    full dataset, advancing by ``test_months`` on each iteration.  Returns a
    DataFrame with one row per window.  ``param_stability_score`` is the
    Pearson correlation of ``train_wr`` and ``test_wr`` across windows
    (NaN when fewer than 2 windows are available).
    """
    df = load_ohlc_csv(csv_path)
    if df.empty:
        return pd.DataFrame()

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

        # Need enough bars for indicator warmup (EMA 200 needs ≥200 bars).
        if len(train_df) < 10 or len(test_df) < 5:
            window_start += pd.DateOffset(months=test_months)
            continue

        train_m = _extract_metrics(_run_on_df(train_df, symbol))
        test_m = _extract_metrics(_run_on_df(test_df, symbol))

        windows.append(
            {
                "window": str(window_start.date()),
                "train_trades": train_m["total"],
                "test_trades": test_m["total"],
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

    # Stability score: Pearson r of train_wr and test_wr across all windows.
    if len(results) >= 2:
        corr = results["train_wr"].corr(results["test_wr"])
        results["param_stability_score"] = round(float(corr) if not pd.isna(corr) else 0.0, 4)
    else:
        results["param_stability_score"] = float("nan")

    return results


def _print_walk_forward_report(results: pd.DataFrame) -> None:
    if results.empty:
        print("No walk-forward windows could be formed (insufficient data).")
        return

    print("=" * 64)
    print("Walk-Forward Results")
    print("=" * 64)
    for _, row in results.iterrows():
        srs_flag = "PASS" if row["srs_criteria_met"] else "FAIL"
        print(
            f"  {row['window']}  "
            f"train_wr={row['train_wr']:.2%}  test_wr={row['test_wr']:.2%}  "
            f"test_avg_r={row['test_avg_r']:.2f}  test_dd={row['test_max_dd']:.1f}%  "
            f"SRS={srs_flag}"
        )
    stability = results["param_stability_score"].iloc[0]
    print("-" * 64)
    print(f"Param stability score: {stability:.3f}  (>0.6 = stable)")
    print("=" * 64)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward optimisation for FX AI Engine")
    parser.add_argument("--csv", required=True, help="Path to OHLC CSV file")
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--train", type=int, default=6, help="Training window in months")
    parser.add_argument("--test", type=int, default=1, help="Test window in months")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not Path(args.csv).exists():
        raise SystemExit(f"CSV not found: {args.csv}")

    results = run_walk_forward(args.csv, args.symbol, args.train, args.test)
    _print_walk_forward_report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
