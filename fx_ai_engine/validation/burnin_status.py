"""Read-only burn-in status for the demo run (plan gate G1).

Usage (from fx_ai_engine/):
    python -m validation.burnin_status            # since 2026-11-03
    python -m validation.burnin_status --since 2026-11-04

G1 passes after 5 trading days unattended with every routed signal in a
terminal state and no unresolved feedback conflicts.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.request
from pathlib import Path

DB = Path(__file__).parents[1] / "database" / "trading_state.db"
TERMINAL = ("CLOSED_WIN", "CLOSED_LOSS", "CLOSED_BREAKEVEN", "REJECTED")


def _health() -> dict | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Demo burn-in status (read-only)")
    parser.add_argument("--since", default="2026-11-03")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = lambda sql, *p: conn.execute(sql, p).fetchall()
    since = args.since

    health = _health()
    print("Engine:", "DOWN (no health response)" if health is None else
          f"{health.get('status')} heartbeat={health.get('last_heartbeat')} halted={health.get('is_trading_halted')} "
          f"open_positions={health.get('open_positions')} balance_usd={health.get('account_balance')}")

    days = q("SELECT COUNT(DISTINCT substr(timestamp,1,10)) FROM decision_funnel_events WHERE timestamp>=?", since)[0][0]
    print(f"Days with decision cycles since {since}: {days} (G1 needs 5 trading days)")

    print("\nTrades by status:")
    for status, n in q("SELECT status, COUNT(*) FROM trades WHERE COALESCE(execution_time, '') >= ? OR trade_id LIKE 'AI_%' GROUP BY status", since):
        print(f"  {status:22} {n}")
    routed = q("SELECT trade_id, symbol, direction, status FROM trades WHERE status NOT IN ('REJECTED') ORDER BY trade_id")
    open_or_pending = [r for r in routed if r[3] not in TERMINAL]
    print(f"Routed trades not yet terminal: {len(open_or_pending)}")
    for row in open_or_pending:
        print("  ", row)

    closed = q("SELECT COUNT(*), COALESCE(SUM(profit_loss),0), COALESCE(AVG(r_multiple),0) FROM trades WHERE status LIKE 'CLOSED_%'")[0]
    print(f"Closed trades: {closed[0]}  net P/L (USD): {closed[1]:.2f}  mean R: {closed[2]:.2f}")

    conflicts = q("SELECT COUNT(*) FROM execution_feedback_conflicts WHERE resolved_at IS NULL")[0][0]
    uncertain = q("SELECT COUNT(*) FROM trade_reservations WHERE state='EXECUTION_UNCERTAIN'")[0][0]
    print(f"Unresolved feedback conflicts: {conflicts}   uncertain reservations: {uncertain}")

    print("\nRisk events (BLOCK) since", since)
    for name, n in q("SELECT rule_name, COUNT(*) FROM risk_events WHERE severity='BLOCK' AND timestamp>=? GROUP BY rule_name ORDER BY 2 DESC", since):
        print(f"  {name:32} {n}")

    print("\nTop rejection reasons since", since)
    for stage, code, n in q("""SELECT stage, reason_code, COUNT(*) FROM decision_funnel_events
            WHERE outcome='REJECT' AND timestamp>=? GROUP BY 1,2 ORDER BY 3 DESC LIMIT 8""", since):
        print(f"  {stage:12} {code:34} {n}")

    g1_ok = days >= 5 and not open_or_pending and conflicts == 0 and uncertain == 0 and health is not None
    print("\nG1:", "PASS" if g1_ok else "not yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
