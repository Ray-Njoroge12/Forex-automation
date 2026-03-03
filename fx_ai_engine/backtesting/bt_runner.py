from __future__ import annotations

import argparse
from pathlib import Path

import backtrader as bt

from backtesting.bt_strategy import AgentBacktestStrategy
from backtesting.data_loader import load_ohlc_csv

STARTING_CASH = 10_000.0


def run_backtest(csv_path: str, symbol: str) -> AgentBacktestStrategy:
    df = load_ohlc_csv(csv_path)

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


def _print_stats(strategy: AgentBacktestStrategy) -> None:
    closed = strategy.results
    total = len(closed)

    sharpe_analysis = strategy.analyzers.sharpe.get_analysis()
    drawdown_analysis = strategy.analyzers.drawdown.get_analysis()

    sharpe_val = sharpe_analysis.get("sharperatio") or 0.0
    max_dd = drawdown_analysis.get("max", {}).get("drawdown") or 0.0

    print("-" * 40)
    print(f"Total trades:   {total}")

    if total == 0:
        print("Win rate:       N/A (no closed trades)")
        print("Avg R:          N/A")
        print(f"Max drawdown:   {max_dd:.2f}%")
        print(f"Sharpe ratio:   {sharpe_val:.2f}")
        print("-" * 40)
        return

    wins = sum(1 for t in closed if t["pnl"] > 0)
    win_rate = wins / total * 100
    avg_r = sum(t["r_multiple"] for t in closed) / total

    print(f"Win rate:       {win_rate:.1f}%")
    print(f"Avg R:          {avg_r:.2f}")
    print(f"Max drawdown:   {max_dd:.2f}%")
    print(f"Sharpe ratio:   {sharpe_val:.2f}")
    print("-" * 40)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtrader harness for FX AI Engine")
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to OHLC CSV with time,open,high,low,close[,volume]",
    )
    parser.add_argument("--symbol", default="EURUSD")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not Path(args.csv).exists():
        raise SystemExit(f"CSV not found: {args.csv}")

    strategy = run_backtest(args.csv, args.symbol)
    _print_stats(strategy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
