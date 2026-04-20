"""Shadow parity automation for mock smoke runs.

Runs two smoke iterations back-to-back:
1) shadow disabled
2) shadow enabled (observe-only)

Then compares newly inserted decision/trade outcomes to ensure that enabling
shadow mode does not change routing/rejection behavior.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Mapping

from database.db import DB_PATH


@dataclass(frozen=True)
class RunSnapshot:
    funnel_counts: dict[tuple[str, str], int]
    trade_status_counts: dict[str, int]
    shadow_event_count: int


def _table_max_id(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COALESCE(MAX(id), 0) AS max_id FROM {table}").fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def _funnel_counts_since(conn: sqlite3.Connection, start_id: int) -> dict[tuple[str, str], int]:
    rows = conn.execute(
        """
        SELECT stage, outcome, COUNT(*) AS n
          FROM decision_funnel_events
         WHERE id > ?
         GROUP BY stage, outcome
        """,
        (start_id,),
    ).fetchall()
    return {(str(row[0]), str(row[1])): int(row[2]) for row in rows}


def _trade_status_counts_since(conn: sqlite3.Connection, start_id: int) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n
          FROM trades
         WHERE id > ?
         GROUP BY status
        """,
        (start_id,),
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _shadow_event_count_since(conn: sqlite3.Connection, start_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ml_shadow_events WHERE id > ?",
        (start_id,),
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def _capture_snapshot(start_ids: Mapping[str, int]) -> RunSnapshot:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return RunSnapshot(
            funnel_counts=_funnel_counts_since(conn, start_ids["decision_funnel_events"]),
            trade_status_counts=_trade_status_counts_since(conn, start_ids["trades"]),
            shadow_event_count=_shadow_event_count_since(conn, start_ids["ml_shadow_events"]),
        )
    finally:
        conn.close()


def _capture_start_ids() -> dict[str, int]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "decision_funnel_events": _table_max_id(conn, "decision_funnel_events"),
            "trades": _table_max_id(conn, "trades"),
            "ml_shadow_events": _table_max_id(conn, "ml_shadow_events"),
        }
    finally:
        conn.close()


def _run_smoke(*, fx_root: Path, env_overrides: Mapping[str, str]) -> RunSnapshot:
    start_ids = _capture_start_ids()

    env = os.environ.copy()
    env.update(env_overrides)
    command = [sys.executable, "main.py", "--mode", "smoke"]
    completed = subprocess.run(command, cwd=fx_root, env=env, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Smoke run failed with exit code {completed.returncode}")

    return _capture_snapshot(start_ids)


def _parity_projection(snapshot: RunSnapshot) -> dict[str, int]:
    funnel = snapshot.funnel_counts
    return {
        "router_routed": funnel.get(("ROUTER", "ROUTED"), 0),
        "router_reject": funnel.get(("ROUTER", "REJECT"), 0),
        "ml_ranker_pass": funnel.get(("ML_RANKER", "PASS"), 0),
        "ml_ranker_reject": funnel.get(("ML_RANKER", "REJECT"), 0),
        "ml_ranker_bypass": funnel.get(("ML_RANKER", "BYPASS"), 0),
        "total_reject_events": sum(
            count for (_stage, outcome), count in funnel.items() if outcome == "REJECT"
        ),
        "trades_pending": snapshot.trade_status_counts.get("PENDING", 0),
        "trades_rejected": snapshot.trade_status_counts.get("REJECTED", 0),
    }


def _format_projection(label: str, projection: Mapping[str, int]) -> str:
    ordered_keys = [
        "router_routed",
        "router_reject",
        "ml_ranker_pass",
        "ml_ranker_reject",
        "ml_ranker_bypass",
        "total_reject_events",
        "trades_pending",
        "trades_rejected",
    ]
    values = " ".join(f"{key}={projection.get(key, 0)}" for key in ordered_keys)
    return f"{label}: {values}"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run mock smoke parity check for observe-only shadow mode.",
    )
    parser.add_argument(
        "--fx-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Path to fx_ai_engine project root (default: current package root)",
    )
    parser.add_argument(
        "--bridge-path",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "mock_mt5_bridge",
        help="Mock bridge path for smoke runs",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    fx_root = args.fx_root.resolve()
    bridge_path = args.bridge_path.resolve()

    shared_env = {
        "USE_MT5_MOCK": "1",
        "MT5_MOCK_BRIDGE_PATH": str(bridge_path),
        "FX_EXPERIMENT_META_LABELER_SHADOW": "0",
        "FX_EXPERIMENT_META_LABELER_CANARY": "0",
        "FX_META_LABELER_CANARY_ENABLED": "0",
        "FX_META_LABELER_CANARY_KILL_SWITCH": "1",
        "FX_META_LABELER_SHADOW_MODEL_PATH": "",
        "FX_META_LABELER_SHADOW_THRESHOLD": "0.55",
    }

    off_env = {
        **shared_env,
        "FX_META_LABELER_SHADOW_ENABLED": "0",
    }
    on_env = {
        **shared_env,
        "FX_META_LABELER_SHADOW_ENABLED": "1",
    }

    snapshot_off = _run_smoke(fx_root=fx_root, env_overrides=off_env)
    snapshot_on = _run_smoke(fx_root=fx_root, env_overrides=on_env)

    projection_off = _parity_projection(snapshot_off)
    projection_on = _parity_projection(snapshot_on)

    print(_format_projection("shadow_off", projection_off))
    print(_format_projection("shadow_on", projection_on))
    print(f"shadow_events_off={snapshot_off.shadow_event_count}")
    print(f"shadow_events_on={snapshot_on.shadow_event_count}")

    if snapshot_on.shadow_event_count == 0:
        print("PARITY_FAIL: shadow run produced no ml_shadow_events rows")
        return 1

    if projection_off != projection_on:
        print("PARITY_FAIL: routing/rejection projections differ between shadow off/on")
        return 1

    print("PARITY_PASS: shadow observe-only mode preserved routing/rejection projections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
