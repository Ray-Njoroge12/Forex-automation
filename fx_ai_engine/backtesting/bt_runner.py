from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from backtesting.data_loader import load_ohlc_csv
from backtesting.simulation_profile import build_simulation_profile
from config_microcapital import MODE_CONFIGS

if TYPE_CHECKING:
    from backtesting.bt_strategy import AgentBacktestStrategy


def _load_backtesting_runtime():
    import backtrader as bt

    from backtesting.bt_strategy import AgentBacktestStrategy

    return bt, AgentBacktestStrategy


def run_backtest_on_df(df, symbol: str, *, mode_id: str | None = None) -> "AgentBacktestStrategy":
    bt, AgentBacktestStrategy = _load_backtesting_runtime()
    profile = build_simulation_profile(mode_id)
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
    cerebro.broker.setcash(profile.starting_cash)
    cerebro.adddata(data, name=symbol)
    cerebro.addstrategy(AgentBacktestStrategy, symbol=symbol, simulation_profile=profile)

    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.0, annualize=True)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    results = cerebro.run()
    return results[0]


def run_backtest(csv_path: str, symbol: str, *, mode_id: str | None = None) -> "AgentBacktestStrategy":
    df = load_ohlc_csv(csv_path)
    return run_backtest_on_df(df, symbol, mode_id=mode_id)


def _print_stats(strategy: AgentBacktestStrategy) -> None:
    closed = strategy.results
    total = len(closed)
    profile = getattr(strategy, "simulation_profile", build_simulation_profile("core_srs"))
    rejected = len(getattr(strategy, "rejected_signals", []))

    sharpe_analysis = strategy.analyzers.sharpe.get_analysis()
    drawdown_analysis = strategy.analyzers.drawdown.get_analysis()

    sharpe_val = sharpe_analysis.get("sharperatio") or 0.0
    max_dd = (
        getattr(strategy, "max_simulated_drawdown_pct", 0.0)
        if profile.realistic_constraints
        else drawdown_analysis.get("max", {}).get("drawdown") or 0.0
    )

    print("-" * 40)
    print(f"Evidence:       {profile.evidence_label} [{profile.evidence_stream}]")
    print(f"Simulation:     {profile.realism_label}")
    print(f"Starting cash:  ${profile.starting_cash:.2f}")
    if profile.realistic_constraints:
        print(
            "Execution:      "
            f"min_lot={profile.min_lot:.2f} lot_step={profile.lot_step:.2f} "
            f"spread={profile.base_spread_pips:.1f} slippage={profile.slippage_pips:.1f} "
            f"commission=${profile.commission_per_lot_usd:.2f}/lot"
        )
    print(f"Total trades:   {total}")
    print(f"Rejected setups:{rejected:>4}")
    funnel_counts = getattr(strategy, "funnel_counts", {})
    if funnel_counts:
        print("Funnel summary:")
        for key in (
            "REGIME:PASS",
            "REGIME:REJECT",
            "TECHNICAL:PASS",
            "TECHNICAL:REJECT",
            "ADVERSARIAL:PASS",
            "ADVERSARIAL:REJECT",
            "PORTFOLIO:PASS",
            "PORTFOLIO:REJECT",
            "FEASIBILITY:PASS",
            "FEASIBILITY:REJECT",
            "FEASIBILITY:BYPASS",
            "ROUTER:ROUTED",
        ):
            if key in funnel_counts:
                print(f"  {key:<20} {funnel_counts[key]}")

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
    parser.add_argument("--policy-mode", choices=sorted(MODE_CONFIGS), default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not Path(args.csv).exists():
        raise SystemExit(f"CSV not found: {args.csv}")

    strategy = run_backtest(args.csv, args.symbol, mode_id=args.policy_mode)
    _print_stats(strategy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
