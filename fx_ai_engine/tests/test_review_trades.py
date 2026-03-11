from __future__ import annotations

import sqlite3

import review_trades as rt


def _setup_review_db(tmp_path):
    db = tmp_path / "review.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, evidence_stream TEXT, policy_mode TEXT, execution_mode TEXT, account_scope TEXT, trade_id TEXT, symbol TEXT, status TEXT, reason_code TEXT, risk_percent REAL, spread_entry REAL, profit_loss REAL, slippage REAL, r_multiple REAL, open_time TEXT, close_time TEXT)"
    )
    conn.execute("CREATE TABLE account_metrics (id INTEGER PRIMARY KEY, evidence_stream TEXT, policy_mode TEXT, execution_mode TEXT, account_scope TEXT)")
    conn.execute("CREATE TABLE risk_events (id INTEGER PRIMARY KEY, evidence_stream TEXT, policy_mode TEXT, execution_mode TEXT, account_scope TEXT)")
    conn.execute("CREATE TABLE decision_funnel_events (id INTEGER PRIMARY KEY, evidence_stream TEXT, policy_mode TEXT, execution_mode TEXT, account_scope TEXT)")
    conn.executemany(
        "INSERT INTO trades (id, evidence_stream, policy_mode, execution_mode, account_scope, trade_id, symbol, status, reason_code, risk_percent, spread_entry, profit_loss, slippage, r_multiple, open_time, close_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1", "t1", "EURUSD", "CLOSED_WIN", "OK", 3.2, 1.1, 10.0, 0.0, 2.2, "2026-01-01", "2026-01-01"),
            (2, rt.EXPERIMENT_STREAM, "core_srs", "mt5", "mt5:demo:1", "t2", "USDJPY", "CLOSED_LOSS", "OK", 3.2, 1.2, -5.0, 0.0, -1.0, "2026-01-02", "2026-01-02"),
            (3, rt.EXPERIMENT_STREAM, "core_srs", "mt5", "mt5:demo:1", "t3", "USDJPY", "PENDING", "OK", 3.2, 1.2, 0.0, 0.0, 0.0, "2026-01-03", None),
            (4, "legacy_unpartitioned", "legacy_unpartitioned", "legacy", "legacy_unpartitioned", "t4", "GBPUSD", "CLOSED_WIN", "OK", 1.0, 0.8, 4.0, 0.0, 1.1, "2026-01-04", "2026-01-04"),
        ],
    )
    conn.executemany(
        "INSERT INTO account_metrics (id, evidence_stream, policy_mode, execution_mode, account_scope) VALUES (?,?,?,?,?)",
        [(1, "runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1"), (2, rt.EXPERIMENT_STREAM, "core_srs", "mt5", "mt5:demo:1"), (3, "legacy_unpartitioned", "legacy_unpartitioned", "legacy", "legacy_unpartitioned")],
    )
    conn.executemany(
        "INSERT INTO risk_events (id, evidence_stream, policy_mode, execution_mode, account_scope) VALUES (?,?,?,?,?)",
        [(1, rt.EXPERIMENT_STREAM, "core_srs", "mt5", "mt5:demo:1"), (2, "runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1"), (3, "legacy_unpartitioned", "legacy_unpartitioned", "legacy", "legacy_unpartitioned")],
    )
    conn.execute(
        "INSERT INTO decision_funnel_events (id, evidence_stream, policy_mode, execution_mode, account_scope) VALUES (?,?,?,?,?)",
        (1, "legacy_unpartitioned", "legacy_unpartitioned", "legacy", "legacy_unpartitioned"),
    )
    conn.commit()
    return conn


def _setup_legacy_review_db(tmp_path):
    db = tmp_path / "legacy_review.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, trade_id TEXT, symbol TEXT, status TEXT, reason_code TEXT, risk_percent REAL, spread_entry REAL, profit_loss REAL, slippage REAL, r_multiple REAL, open_time TEXT, close_time TEXT)"
    )
    conn.executemany(
        "INSERT INTO trades (id, trade_id, symbol, status, reason_code, risk_percent, spread_entry, profit_loss, slippage, r_multiple, open_time, close_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "legacy_1", "EURUSD", "CLOSED_WIN", "OK", 3.2, 1.1, 10.0, 0.0, 2.2, "2026-01-01", "2026-01-01"),
            (2, "legacy_2", "USDJPY", "CLOSED_LOSS", "OK", 3.2, 1.2, -5.0, 0.0, -1.0, "2026-01-02", "2026-01-02"),
            (3, "legacy_3", "GBPUSD", "PENDING", "OK", 3.2, 1.0, 0.0, 0.0, 0.0, "2026-01-03", None),
        ],
    )
    conn.commit()
    return conn


def test_list_evidence_streams_unions_tables(tmp_path):
    conn = _setup_review_db(tmp_path)
    try:
        assert rt.list_evidence_streams(conn) == [
            "legacy_unpartitioned",
            "runtime_mt5_core_srs",
            rt.EXPERIMENT_STREAM,
        ]
    finally:
        conn.close()


def test_load_recent_trades_filters_by_evidence_stream(tmp_path):
    conn = _setup_review_db(tmp_path)
    try:
        df = rt.load_recent_trades(conn, evidence_stream=rt.EXPERIMENT_STREAM)
        assert list(df["trade_id"]) == ["t3", "t2"]
        assert set(df["evidence_stream"]) == {rt.EXPERIMENT_STREAM}
    finally:
        conn.close()


def test_load_recent_trades_defaults_to_clean_core_srs_mt5_scope(tmp_path):
    conn = _setup_review_db(tmp_path)
    try:
        df = rt.load_recent_trades(conn)
        assert list(df["trade_id"]) == ["t3", "t2", "t1"]
        assert set(df["policy_mode"]) == {"core_srs"}
        assert set(df["execution_mode"]) == {"mt5"}
    finally:
        conn.close()


def test_load_profit_summary_scopes_win_rate_and_avg_r(tmp_path):
    conn = _setup_review_db(tmp_path)
    try:
        df = rt.load_profit_summary(conn, evidence_stream=rt.EXPERIMENT_STREAM)
        row = df.iloc[0]
        assert int(row["total_trades"]) == 1
        assert int(row["winning_trades"]) == 0
        assert float(row["win_rate_pct"]) == 0.0
        assert float(row["avg_r"]) == -1.0
    finally:
        conn.close()


def test_load_legacy_partition_counts_reports_all_partitioned_tables(tmp_path):
    conn = _setup_review_db(tmp_path)
    try:
        counts = rt.load_legacy_partition_counts(conn)
        assert counts == {
            "trades": 1,
            "account_metrics": 1,
            "risk_events": 1,
            "decision_funnel_events": 1,
            "total": 4,
        }
    finally:
        conn.close()


def test_load_recent_trades_falls_back_for_legacy_db_without_partition_columns(tmp_path):
    conn = _setup_legacy_review_db(tmp_path)
    try:
        df = rt.load_recent_trades(conn)
        assert list(df["trade_id"]) == ["legacy_3", "legacy_2", "legacy_1"]
    finally:
        conn.close()


def test_load_profit_summary_falls_back_for_legacy_db_without_partition_columns(tmp_path):
    conn = _setup_legacy_review_db(tmp_path)
    try:
        df = rt.load_profit_summary(conn)
        row = df.iloc[0]
        assert int(row["total_trades"]) == 2
        assert int(row["winning_trades"]) == 1
        assert float(row["win_rate_pct"]) == 50.0
        assert round(float(row["avg_r"]), 6) == 0.6
    finally:
        conn.close()
