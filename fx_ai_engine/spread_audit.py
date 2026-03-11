from __future__ import annotations

import argparse
import sqlite3

import pandas as pd

from database.db import DB_PATH

DEFAULT_POLICY_MODE = "core_srs"
DEFAULT_EXECUTION_MODE = "mt5"


def load_spread_samples(
    conn: sqlite3.Connection,
    *,
    policy_mode: str | None = DEFAULT_POLICY_MODE,
    execution_mode: str | None = DEFAULT_EXECUTION_MODE,
    include_all: bool = False,
) -> pd.DataFrame:
    clauses = ["spread_entry IS NOT NULL", "COALESCE(spread_entry, 0.0) > 0"]
    params: list[object] = []
    if not include_all and policy_mode is not None:
        clauses.append("policy_mode = ?")
        params.append(policy_mode)
    if not include_all and execution_mode is not None:
        clauses.append("execution_mode = ?")
        params.append(execution_mode)
    query = f"""
        SELECT symbol,
               spread_entry,
               COALESCE(is_london_session, 0) AS is_london_session,
               COALESCE(is_newyork_session, 0) AS is_newyork_session,
               open_time
          FROM trades
         WHERE {' AND '.join(clauses)}
    """
    return pd.read_sql_query(query, conn, params=tuple(params))


def summarize_spreads(samples: pd.DataFrame) -> pd.DataFrame:
    if samples.empty:
        return pd.DataFrame(
            columns=["symbol", "session_bucket", "samples", "median", "p90", "p95", "max"]
        )

    labeled = samples.copy()
    labeled["session_bucket"] = "other"
    labeled.loc[labeled["is_london_session"] == 1, "session_bucket"] = "london"
    labeled.loc[
        (labeled["is_newyork_session"] == 1) & (labeled["is_london_session"] == 0),
        "session_bucket",
    ] = "newyork"
    grouped = labeled.groupby(["symbol", "session_bucket"])["spread_entry"]
    summary = grouped.agg(
        samples="count",
        median="median",
        max="max",
    ).reset_index()
    percentiles = grouped.quantile([0.90, 0.95]).unstack(fill_value=0.0).reset_index()
    percentiles.columns = ["symbol", "session_bucket", "p90", "p95"]
    merged = summary.merge(percentiles, on=["symbol", "session_bucket"], how="left")
    return merged[["symbol", "session_bucket", "samples", "median", "p90", "p95", "max"]]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spread statistics audit from logged trades")
    parser.add_argument("--all", action="store_true", help="Disable the default core_srs + mt5 scope")
    parser.add_argument("--policy-mode", default=DEFAULT_POLICY_MODE, help="Policy mode to inspect")
    parser.add_argument("--execution-mode", default=DEFAULT_EXECUTION_MODE, help="Execution mode to inspect")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with sqlite3.connect(DB_PATH) as conn:
        samples = load_spread_samples(
            conn,
            policy_mode=args.policy_mode,
            execution_mode=args.execution_mode,
            include_all=args.all,
        )
    summary = summarize_spreads(samples)
    if summary.empty:
        print("No spread samples found for the requested scope.")
        return 1
    print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
