from __future__ import annotations

import argparse
from pathlib import Path

from backtesting.bt_runner import run_backtest
from config_microcapital import MODE_CONFIGS


def _build_candidate_overrides(args: argparse.Namespace) -> dict:
    regime: dict[str, float] = {}
    technical: dict[str, float] = {}

    if args.regime_adx_no_trade_below is not None:
        regime["adx_no_trade_below"] = args.regime_adx_no_trade_below
    if args.regime_adx_transition_below is not None:
        regime["adx_transition_below"] = args.regime_adx_transition_below
    if args.tech_pullback_buffer_pips is not None:
        technical["pullback_buffer_pips"] = args.tech_pullback_buffer_pips
    if args.tech_buy_rsi_min is not None:
        technical["buy_rsi_min"] = args.tech_buy_rsi_min
    if args.tech_buy_rsi_max is not None:
        technical["buy_rsi_max"] = args.tech_buy_rsi_max
    if args.tech_sell_rsi_min is not None:
        technical["sell_rsi_min"] = args.tech_sell_rsi_min
    if args.tech_sell_rsi_max is not None:
        technical["sell_rsi_max"] = args.tech_sell_rsi_max

    thresholds: dict[str, dict[str, float]] = {}
    if regime:
        thresholds["REGIME"] = regime
    if technical:
        thresholds["TECHNICAL"] = technical
    return {"AGENT_THRESHOLDS": thresholds} if thresholds else {}


def _summarize(label: str, strategy) -> dict[str, object]:
    closed = strategy.results
    total = len(closed)
    wins = sum(1 for trade in closed if trade["pnl"] > 0)
    win_rate = (wins / total * 100.0) if total else 0.0
    avg_r = (sum(trade["r_multiple"] for trade in closed) / total) if total else 0.0
    funnel_counts = getattr(strategy, "funnel_counts", {})
    return {
        "label": label,
        "trades": total,
        "win_rate_pct": round(win_rate, 2),
        "avg_r": round(avg_r, 3),
        "max_drawdown_pct": round(getattr(strategy, "max_simulated_drawdown_pct", 0.0), 3),
        "regime_pass": int(funnel_counts.get("REGIME:PASS", 0)),
        "regime_reject": int(funnel_counts.get("REGIME:REJECT", 0)),
        "technical_pass": int(funnel_counts.get("TECHNICAL:PASS", 0)),
        "technical_skip": int(funnel_counts.get("TECHNICAL:SKIP", 0)),
        "technical_reject": int(funnel_counts.get("TECHNICAL:REJECT", 0)),
        "routed": int(funnel_counts.get("ROUTER:ROUTED", 0)),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline threshold audit for FX AI Engine")
    parser.add_argument("--csv", required=True, help="Path to OHLC CSV with time,open,high,low,close[,volume]")
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--policy-mode", choices=sorted(MODE_CONFIGS), default=None)
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--regime-adx-no-trade-below", type=float)
    parser.add_argument("--regime-adx-transition-below", type=float)
    parser.add_argument("--tech-pullback-buffer-pips", type=float)
    parser.add_argument("--tech-buy-rsi-min", type=float)
    parser.add_argument("--tech-buy-rsi-max", type=float)
    parser.add_argument("--tech-sell-rsi-min", type=float)
    parser.add_argument("--tech-sell-rsi-max", type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not Path(args.csv).exists():
        raise SystemExit(f"CSV not found: {args.csv}")

    baseline = run_backtest(args.csv, args.symbol, mode_id=args.policy_mode)
    summaries = [_summarize("baseline", baseline)]
    overrides = _build_candidate_overrides(args)
    if overrides:
        candidate = run_backtest(
            args.csv,
            args.symbol,
            mode_id=args.policy_mode,
            agent_threshold_overrides=overrides,
        )
        summaries.append(_summarize(args.label, candidate))

    print("Threshold audit summary")
    print("-" * 100)
    print(
        f"{'label':<16} {'trades':>6} {'win_rate%':>10} {'avg_r':>8} {'max_dd%':>9} "
        f"{'reg_pass':>9} {'reg_rej':>8} {'tech_pass':>10} {'tech_skip':>10} {'tech_rej':>9} {'routed':>8}"
    )
    for row in summaries:
        print(
            f"{row['label']:<16} {row['trades']:>6} {row['win_rate_pct']:>10.2f} {row['avg_r']:>8.3f} "
            f"{row['max_drawdown_pct']:>9.3f} {row['regime_pass']:>9} {row['regime_reject']:>8} "
            f"{row['technical_pass']:>10} {row['technical_skip']:>10} {row['technical_reject']:>9} {row['routed']:>8}"
        )
    if overrides:
        print("Candidate overrides:")
        print(overrides)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())