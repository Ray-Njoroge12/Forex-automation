"""Read-only inventory of unresolved local execution evidence.

This does not query MT5 and cannot establish whether an order was executed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "database" / "trading_state.db"


def build_inventory(path: Path, bridge_dir: Path | None = None) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"database missing: {path}")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        reservations = conn.execute("""SELECT r.reservation_id, r.trade_id, r.symbol, r.state,
            r.reason_code, r.risk_percent, r.created_at, r.updated_at, r.expires_at,
            t.evidence_stream, t.account_scope, t.status AS trade_status,
            t.trade_ticket, t.position_ticket,
            CASE WHEN julianday(r.expires_at) IS NULL OR julianday(r.expires_at) <= julianday('now')
                 THEN 1 ELSE 0 END AS expired_or_invalid
            FROM trade_reservations r LEFT JOIN trades t ON r.trade_id=t.trade_id
            WHERE r.state IN ('RESERVED', 'ROUTED', 'EXECUTION_UNCERTAIN')
            ORDER BY r.created_at, r.trade_id""").fetchall()
        conflicts = conn.execute("""SELECT evidence_stream, account_scope, trade_id, reason,
            dedupe_key, created_at FROM execution_feedback_conflicts WHERE resolved_at IS NULL
            ORDER BY created_at, id""").fetchall()
    artifacts = None
    if bridge_dir is not None:
        if not (bridge_dir / "feedback").is_dir() or not (bridge_dir / "pending_signals").is_dir():
            raise FileNotFoundError(f"bridge feedback or pending_signals directory missing: {bridge_dir}")
        artifacts = {
            "bridge_dir": str(bridge_dir.resolve()),
            "submission_markers": sorted(p.name for p in (bridge_dir / "feedback").glob("submission_*.lock")),
            "pending_signals": sorted(p.name for p in (bridge_dir / "pending_signals").glob("*.json")),
            "quarantined_feedback": sorted(p.name for p in (bridge_dir / "feedback" / "quarantine").glob("execution_*.json")),
        }
    return {"database": str(path.resolve()), "broker_verified": False, "bridge_artifacts": artifacts,
            "active_reservations": [dict(row) for row in reservations],
            "unresolved_feedback_conflicts": [dict(row) for row in conflicts]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--bridge-dir", type=Path, help="Inspect artifacts in this bridge directory without changing them")
    args = parser.parse_args(argv)
    try:
        report = build_inventory(args.database, args.bridge_dir)
    except (OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Cannot inspect reconciliation inventory: {exc}. Run the current database migrations before inspection.\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
