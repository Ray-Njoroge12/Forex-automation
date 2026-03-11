from __future__ import annotations

import argparse
import sqlite3
from typing import Any

import pandas as pd

from core.evidence import LEGACY_UNPARTITIONED_ACCOUNT_SCOPE, LEGACY_UNPARTITIONED_STREAM
from database.db import DB_PATH, archive_legacy_partition_rows

DEFAULT_POLICY_MODE = "core_srs"
DEFAULT_EXECUTION_MODE = "mt5"
EXPERIMENT_STREAM = "runtime_mt5_core_srs__pair_selective_rising_adx_relax"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _resolve_scope_filters(
    *,
    evidence_stream: str | None,
    policy_mode: str | None,
    execution_mode: str | None,
    account_scope: str | None,
    include_all: bool,
) -> dict[str, str | None]:
    filters = {
        "evidence_stream": evidence_stream,
        "policy_mode": policy_mode,
        "execution_mode": execution_mode,
        "account_scope": account_scope,
    }
    if include_all:
        return filters
    if any(value is not None for value in filters.values()):
        return filters
    return {
        "evidence_stream": None,
        "policy_mode": DEFAULT_POLICY_MODE,
        "execution_mode": DEFAULT_EXECUTION_MODE,
        "account_scope": None,
    }


def _apply_trade_filters(
    conn: sqlite3.Connection,
    *,
    evidence_stream: str | None,
    policy_mode: str | None,
    execution_mode: str | None,
    account_scope: str | None,
    include_all: bool,
) -> tuple[str, tuple[Any, ...], dict[str, str | None]]:
    filters = _resolve_scope_filters(
        evidence_stream=evidence_stream,
        policy_mode=policy_mode,
        execution_mode=execution_mode,
        account_scope=account_scope,
        include_all=include_all,
    )
    columns = _column_names(conn, "trades")
    clauses: list[str] = []
    params: list[Any] = []
    allow_legacy_scope_fallback = (
        not include_all
        and evidence_stream is None
        and policy_mode is None
        and execution_mode is None
        and account_scope is None
    )
    for column, value in filters.items():
        if value is None:
            continue
        if column not in columns:
            if allow_legacy_scope_fallback:
                continue
            raise sqlite3.OperationalError(f"trades table is missing required column: {column}")
        clauses.append(f"{column} = ?")
        params.append(value)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, tuple(params), filters


def _legacy_partition_where(columns: set[str]) -> tuple[str | None, tuple[Any, ...]]:
    required = {"evidence_stream", "policy_mode", "execution_mode", "account_scope"}
    if not required <= columns:
        return None, ()
    return (
        "WHERE COALESCE(NULLIF(TRIM(evidence_stream), ''), '') = ? "
        "OR COALESCE(NULLIF(TRIM(policy_mode), ''), '') = ? "
        "OR COALESCE(NULLIF(TRIM(execution_mode), ''), '') = 'legacy' "
        "OR COALESCE(NULLIF(TRIM(account_scope), ''), '') = ?",
        (
            LEGACY_UNPARTITIONED_STREAM,
            LEGACY_UNPARTITIONED_STREAM,
            LEGACY_UNPARTITIONED_ACCOUNT_SCOPE,
        ),
    )


def _trade_select_list(conn: sqlite3.Connection) -> str:
    columns = _column_names(conn, "trades")
    select_fields = [
        "id",
        "COALESCE(evidence_stream, '') AS evidence_stream" if "evidence_stream" in columns else "'' AS evidence_stream",
        "COALESCE(policy_mode, '') AS policy_mode" if "policy_mode" in columns else "'' AS policy_mode",
        "COALESCE(execution_mode, '') AS execution_mode" if "execution_mode" in columns else "'' AS execution_mode",
        "COALESCE(account_scope, '') AS account_scope" if "account_scope" in columns else "'' AS account_scope",
        "trade_id",
        "symbol",
        "status",
        "reason_code",
        "risk_percent",
        "spread_entry",
        "profit_loss",
        "slippage",
        "r_multiple",
        "open_time",
        "close_time",
    ]
    return ",\n            ".join(select_fields)


def list_evidence_streams(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT evidence_stream
          FROM (
                SELECT evidence_stream FROM trades
                UNION ALL
                SELECT evidence_stream FROM account_metrics
                UNION ALL
                SELECT evidence_stream FROM risk_events
          )
         WHERE COALESCE(TRIM(evidence_stream), '') <> ''
         ORDER BY evidence_stream ASC
        """
    ).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def load_recent_trades(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    evidence_stream: str | None = None,
    policy_mode: str | None = None,
    execution_mode: str | None = None,
    account_scope: str | None = None,
    include_all: bool = False,
) -> pd.DataFrame:
    where_sql, params, _filters = _apply_trade_filters(
        conn,
        evidence_stream=evidence_stream,
        policy_mode=policy_mode,
        execution_mode=execution_mode,
        account_scope=account_scope,
        include_all=include_all,
    )
    query = f"""
        SELECT
            {_trade_select_list(conn)}
        FROM trades
        {where_sql}
        ORDER BY id DESC
        LIMIT ?
    """
    return pd.read_sql_query(query, conn, params=(*params, limit))


def load_profit_summary(
    conn: sqlite3.Connection,
    *,
    evidence_stream: str | None = None,
    policy_mode: str | None = None,
    execution_mode: str | None = None,
    account_scope: str | None = None,
    include_all: bool = False,
) -> pd.DataFrame:
    where_sql, params, _filters = _apply_trade_filters(
        conn,
        evidence_stream=evidence_stream,
        policy_mode=policy_mode,
        execution_mode=execution_mode,
        account_scope=account_scope,
        include_all=include_all,
    )
    query = f"""
        SELECT
            COALESCE(SUM(profit_loss), 0.0) AS total_profit,
            COUNT(trade_id) AS total_trades,
            COALESCE(SUM(CASE WHEN profit_loss > 0 THEN 1 ELSE 0 END), 0) AS winning_trades,
            COALESCE(100.0 * SUM(CASE WHEN profit_loss > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(trade_id), 0), 0.0) AS win_rate_pct,
            COALESCE(AVG(r_multiple), 0.0) AS avg_r
        FROM trades
        {where_sql if where_sql else 'WHERE 1=1'}
          AND status LIKE 'CLOSED%'
    """
    return pd.read_sql_query(query, conn, params=params)


def load_legacy_partition_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("trades", "account_metrics", "risk_events", "decision_funnel_events"):
        columns = _column_names(conn, table)
        where_sql, params = _legacy_partition_where(columns)
        if where_sql is None:
            counts[table] = 0
            continue
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} {where_sql}",
            params,
        ).fetchone()
        counts[table] = int(row[0] or 0)
    counts["total"] = sum(counts.values())
    return counts


def format_scope_label(scope_filters: dict[str, str | None]) -> str:
    tokens = [f"{key}={value}" for key, value in scope_filters.items() if value is not None]
    return ", ".join(tokens) if tokens else "ALL"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review trade history from SQLite")
    parser.add_argument("--limit", type=int, default=100, help="Max recent trades to display (default: 100)")
    parser.add_argument("--evidence-stream", default=None, help="Optional evidence_stream filter")
    parser.add_argument("--policy-mode", default=None, help="Optional policy_mode filter")
    parser.add_argument("--execution-mode", default=None, help="Optional execution_mode filter")
    parser.add_argument("--account-scope", default=None, help="Optional account_scope filter")
    parser.add_argument("--all", action="store_true", help="Disable the default core_srs + mt5 baseline scope")
    parser.add_argument(
        "--archive-legacy",
        action="store_true",
        help="Archive legacy_unpartitioned rows before reporting",
    )
    parser.add_argument(
        "--experiment",
        action="store_true",
        help="Shortcut for --evidence-stream runtime_mt5_core_srs__pair_selective_rising_adx_relax",
    )
    parser.add_argument("--list-evidence-streams", action="store_true", help="List distinct evidence streams and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    evidence_stream = EXPERIMENT_STREAM if args.experiment else args.evidence_stream
    scope_filters = _resolve_scope_filters(
        evidence_stream=evidence_stream,
        policy_mode=args.policy_mode,
        execution_mode=args.execution_mode,
        account_scope=args.account_scope,
        include_all=args.all,
    )
    try:
        if args.archive_legacy:
            archived_counts = archive_legacy_partition_rows(archive_reason="review_trades_cli")
            print("Archived legacy partition rows:")
            for table in ("trades", "account_metrics", "risk_events", "decision_funnel_events", "total"):
                print(f" - {table}: {archived_counts.get(table, 0)}")

        conn = sqlite3.connect(DB_PATH)
        print(f"DB: {DB_PATH}")
        if args.list_evidence_streams:
            print("Available evidence streams:")
            for stream in list_evidence_streams(conn):
                print(f" - {stream}")
            return 0

        df = load_recent_trades(
            conn,
            limit=args.limit,
            evidence_stream=evidence_stream,
            policy_mode=args.policy_mode,
            execution_mode=args.execution_mode,
            account_scope=args.account_scope,
            include_all=args.all,
        )
        print(f"Trade scope: {format_scope_label(scope_filters)}")
        print("Recent Trade Proposals and Executions:")
        print(df.to_string(index=False) if not df.empty else "<no matching trades>")

        df_profit = load_profit_summary(
            conn,
            evidence_stream=evidence_stream,
            policy_mode=args.policy_mode,
            execution_mode=args.execution_mode,
            account_scope=args.account_scope,
            include_all=args.all,
        )
        print("\nProfitability Summary:")
        print(df_profit.to_string(index=False))

        legacy_counts = load_legacy_partition_counts(conn)
        print("\nLegacy Partition Counts:")
        for table in ("trades", "account_metrics", "risk_events", "decision_funnel_events", "total"):
            print(f" - {table}: {legacy_counts.get(table, 0)}")
    except Exception as exc:
        print("Failed:", exc)
        return 1
    finally:
        if "conn" in locals() and conn:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
