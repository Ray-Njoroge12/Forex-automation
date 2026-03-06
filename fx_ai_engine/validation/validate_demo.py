"""Pre-live 30-day demo validation harness.

Reads the SQLite trading database and evaluates whether the 30-day demo
period meets SRS v1 acceptance criteria.

Usage:
    python -m validation.validate_demo           # last 30 days (default)
    python -m validation.validate_demo --days 7  # spot check

SRS v1 Acceptance Criteria (§12.2):
    >=25 trades | >=45% win rate | >=2.0 avg R | <=15% max drawdown

SRS v1 Abort Criteria (§12.3):
    drawdown >20% | win rate <40% | avg R <1.8

Verdicts:
    PASS    -- all criteria met; ready for live capital
    ABORT   -- abort threshold triggered; stop demo immediately
    WARN    -- not all criteria met but no abort trigger
    PENDING -- insufficient data (fewer than 25 trades)
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "database" / "trading_state.db"

# SRS v1 §12.2 -- Acceptance
MIN_TRADES = 25
MIN_WIN_RATE = 0.45
MIN_AVG_R = 2.0
MAX_DRAWDOWN = 0.15

# SRS v1 §12.3 -- Abort
ABORT_WIN_RATE = 0.40
ABORT_AVG_R = 1.8
ABORT_DRAWDOWN = 0.20


def _load_trades(days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT trade_id, symbol, direction, r_multiple, profit_loss,
               status, close_time, reason_code
          FROM trades
         WHERE status LIKE 'CLOSED%'
           AND close_time >= ?
         ORDER BY close_time ASC
        """,
        (since,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _load_equity_curve(days: int) -> list[float]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT equity FROM account_metrics WHERE timestamp >= ? ORDER BY timestamp ASC",
        (since,),
    ).fetchall()
    conn.close()
    return [float(row["equity"]) for row in rows if row["equity"] is not None]


def _compute_max_drawdown(equity_curve: list[float]) -> float:
    """Return maximum peak-to-trough drawdown as a fraction (0.0-1.0)."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)
    return round(max_dd, 4)


def _compute_metrics(trades: list[dict], equity_curve: list[float]) -> dict:
    total = len(trades)
    if total == 0:
        return {"total_trades": 0, "win_rate": 0.0, "avg_r": 0.0, "avg_r_winners": 0.0,
                "max_drawdown": _compute_max_drawdown(equity_curve),
                "wins": 0, "losses": 0, "r_multiples": []}
    r_multiples = [float(t["r_multiple"]) for t in trades if t["r_multiple"] is not None]
    wins = sum(1 for r in r_multiples if r > 0)
    losses = total - wins
    win_rate = wins / total if total > 0 else 0.0
    winning_r = [r for r in r_multiples if r > 0]
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0
    avg_r_winners = sum(winning_r) / len(winning_r) if winning_r else 0.0
    warnings: list[str] = []
    zero_r = sum(1 for r in r_multiples if abs(r) < 1e-9)
    if r_multiples and zero_r == len(r_multiples):
        warnings.append("All closed trades have zero r_multiple; execution/exit analytics may be incomplete.")
    zero_pnl = sum(1 for t in trades if abs(float(t.get("profit_loss", 0.0))) < 1e-9)
    if trades and zero_pnl == len(trades):
        warnings.append("All closed trades have zero PnL; verify exit feedback ingestion.")
    return {
        "total_trades": total,
        "win_rate": round(win_rate, 4),
        "avg_r": round(avg_r, 4),
        "avg_r_winners": round(avg_r_winners, 4),
        "max_drawdown": _compute_max_drawdown(equity_curve),
        "wins": wins,
        "losses": losses,
        "r_multiples": r_multiples,
        "warnings": warnings,
    }


def _per_symbol_breakdown(trades: list[dict]) -> dict[str, dict]:
    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        groups[t["symbol"]].append(t)
    result = {}
    for sym, sym_trades in sorted(groups.items()):
        r_vals = [float(t["r_multiple"]) for t in sym_trades if t["r_multiple"] is not None]
        wins = sum(1 for r in r_vals if r > 0)
        result[sym] = {
            "trades": len(sym_trades),
            "wins": wins,
            "win_rate": round(wins / len(sym_trades), 3) if sym_trades else 0.0,
            "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else 0.0,
        }
    return result


def determine_verdict(metrics: dict) -> tuple[str, list[str]]:
    """Return (verdict, list_of_reason_strings)."""
    reasons: list[str] = []

    # Abort checks first (highest priority -- ordered by severity)
    if metrics["max_drawdown"] > ABORT_DRAWDOWN:
        reasons.append(f"ABORT: drawdown {metrics['max_drawdown']:.1%} > {ABORT_DRAWDOWN:.0%} limit")
        return "ABORT", reasons
    if metrics["total_trades"] >= MIN_TRADES and metrics["win_rate"] < ABORT_WIN_RATE:
        reasons.append(f"ABORT: win rate {metrics['win_rate']:.1%} < {ABORT_WIN_RATE:.0%} abort threshold")
        return "ABORT", reasons
    if metrics["total_trades"] >= MIN_TRADES and metrics["avg_r"] < ABORT_AVG_R:
        reasons.append(f"ABORT: avg R {metrics['avg_r']:.2f} < {ABORT_AVG_R} abort threshold")
        return "ABORT", reasons

    # Pending -- not enough trades
    if metrics["total_trades"] < MIN_TRADES:
        reasons.append(f"PENDING: {metrics['total_trades']}/{MIN_TRADES} trades completed")
        return "PENDING", reasons

    # Acceptance criteria
    fails: list[str] = []
    if metrics["win_rate"] < MIN_WIN_RATE:
        fails.append(f"win rate {metrics['win_rate']:.1%} < {MIN_WIN_RATE:.0%} required")
    if metrics["avg_r"] < MIN_AVG_R:
        fails.append(f"avg R {metrics['avg_r']:.2f} < {MIN_AVG_R} required")
    if metrics["max_drawdown"] > MAX_DRAWDOWN:
        fails.append(f"drawdown {metrics['max_drawdown']:.1%} > {MAX_DRAWDOWN:.0%} limit")

    if fails:
        reasons.extend(fails)
        return "WARN", reasons

    reasons.append("All SRS v1 S12.2 acceptance criteria satisfied.")
    return "PASS", reasons


def check_abort_criteria(
    drawdown_pct: float,
    win_rate: float,
    avg_r: float,
    total_trades: int,
) -> dict:
    """Programmatic abort check per SRS v1 §12.3.

    Returns dict with keys:
      abort (bool) — True if any abort threshold is breached
      reason (str) — human-readable reason, empty string if no abort

    Win rate and avg R are only evaluated once >= 25 trades have closed.
    Drawdown is always evaluated regardless of trade count.
    """
    if drawdown_pct > ABORT_DRAWDOWN:
        return {
            "abort": True,
            "reason": f"drawdown {drawdown_pct:.1%} exceeds {ABORT_DRAWDOWN:.0%} abort threshold",
        }
    if total_trades >= MIN_TRADES:
        if win_rate < ABORT_WIN_RATE:
            return {
                "abort": True,
                "reason": f"win rate {win_rate:.1%} below {ABORT_WIN_RATE:.0%} abort threshold",
            }
        if avg_r < ABORT_AVG_R:
            return {
                "abort": True,
                "reason": f"avg R {avg_r:.2f} below {ABORT_AVG_R} abort threshold",
            }
    return {"abort": False, "reason": ""}


def _print_report(
    metrics: dict,
    breakdown: dict,
    verdict: str,
    reasons: list[str],
    days: int,
) -> None:
    bar = "=" * 62
    print(f"\n{bar}")
    print(f"  FX AI Engine -- Pre-Live Validation Report")
    print(f"  Period: last {days} days  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{bar}")
    print(f"\n  Core Metrics                   Value       Requirement")
    print(f"  {'-' * 52}")
    print(f"  Total Trades               {metrics['total_trades']:>9}       >=25")
    print(f"  Win Rate                   {metrics['win_rate']:>8.1%}       >=45%")
    print(f"  Average R-Multiple (All)   {metrics['avg_r']:>9.2f}       >=2.0")
    print(f"  Average R-Multiple (Wins)  {metrics['avg_r_winners']:>9.2f}       diagnostic")
    print(f"  Max Drawdown               {metrics['max_drawdown']:>8.1%}       <=15%")
    if breakdown:
        print(f"\n  Per-Symbol    Trades   Wins      WR   Avg R")
        print(f"  {'-' * 46}")
        for sym, d in breakdown.items():
            print(f"  {sym:<12} {d['trades']:>6} {d['wins']:>6} {d['win_rate']:>7.1%} {d['avg_r']:>7.2f}")
    print(f"\n{bar}")
    print(f"  VERDICT: {verdict}")
    for r in reasons:
        print(f"    -> {r}")
    for warning in metrics.get("warnings", []):
        print(f"    -> DATA WARNING: {warning}")
    print(f"{bar}\n")


def run_validation(days: int = 30) -> tuple[str, dict]:
    """Run full validation. Returns (verdict, metrics) for programmatic use."""
    trades = _load_trades(days)
    equity_curve = _load_equity_curve(days)
    metrics = _compute_metrics(trades, equity_curve)
    breakdown = _per_symbol_breakdown(trades)
    verdict, reasons = determine_verdict(metrics)
    _print_report(metrics, breakdown, verdict, reasons, days)
    return verdict, metrics


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="FX AI Engine pre-live validation")
    p.add_argument("--days", type=int, default=30, help="Days to analyse (default: 30)")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    verdict, _ = run_validation(args.days)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
