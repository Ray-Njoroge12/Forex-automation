from __future__ import annotations

import sqlite3

import spread_audit as sa


def _setup_spread_db(tmp_path):
    db = tmp_path / "spread.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            spread_entry REAL,
            is_london_session INTEGER,
            is_newyork_session INTEGER,
            open_time TEXT,
            policy_mode TEXT,
            execution_mode TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO trades (
            id, symbol, spread_entry, is_london_session, is_newyork_session,
            open_time, policy_mode, execution_mode
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        [
            (1, "EURUSD", 1.1, 1, 0, "2026-03-01T10:00:00+00:00", "core_srs", "mt5"),
            (2, "EURUSD", 1.4, 1, 0, "2026-03-01T11:00:00+00:00", "core_srs", "mt5"),
            (3, "GBPUSD", 2.2, 0, 1, "2026-03-01T15:00:00+00:00", "core_srs", "mt5"),
            (4, "GBPUSD", 3.0, 0, 1, "2026-03-01T16:00:00+00:00", "legacy_unpartitioned", "legacy"),
        ],
    )
    conn.commit()
    return conn


def test_load_spread_samples_defaults_to_clean_scope(tmp_path):
    conn = _setup_spread_db(tmp_path)
    try:
        samples = sa.load_spread_samples(conn)
        assert list(samples["symbol"]) == ["EURUSD", "EURUSD", "GBPUSD"]
    finally:
        conn.close()


def test_summarize_spreads_groups_by_symbol_and_session(tmp_path):
    conn = _setup_spread_db(tmp_path)
    try:
        summary = sa.summarize_spreads(sa.load_spread_samples(conn, include_all=True))
        eurusd = summary[(summary["symbol"] == "EURUSD") & (summary["session_bucket"] == "london")].iloc[0]
        gbpusd = summary[(summary["symbol"] == "GBPUSD") & (summary["session_bucket"] == "newyork")].iloc[0]
        assert int(eurusd["samples"]) == 2
        assert float(eurusd["median"]) == 1.25
        assert int(gbpusd["samples"]) == 2
        assert float(gbpusd["max"]) == 3.0
    finally:
        conn.close()
