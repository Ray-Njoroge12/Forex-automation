from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from analyze_tendencies import ScopeFilters, build_tendency_report


def _setup_db(tmp_path):
    db = tmp_path / "tendencies.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            trade_id TEXT,
            symbol TEXT,
            status TEXT,
            reason_code TEXT,
            profit_loss REAL,
            r_multiple REAL,
            market_regime TEXT,
            spread_entry REAL,
            risk_reward REAL,
            rsi_slope REAL,
            is_london_session INTEGER,
            is_newyork_session INTEGER,
            open_time TEXT,
            close_time TEXT,
            evidence_stream TEXT,
            policy_mode TEXT,
            execution_mode TEXT,
            account_scope TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE decision_funnel_events (
            id INTEGER PRIMARY KEY,
            decision_time TEXT,
            symbol TEXT,
            stage TEXT,
            outcome TEXT,
            reason_code TEXT,
            trade_id TEXT,
            details TEXT,
            evidence_stream TEXT,
            policy_mode TEXT,
            execution_mode TEXT,
            account_scope TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE risk_events (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            rule_name TEXT,
            severity TEXT,
            reason TEXT,
            trade_id TEXT,
            evidence_stream TEXT,
            policy_mode TEXT,
            execution_mode TEXT,
            account_scope TEXT
        )
        """
    )
    return conn, now


def test_build_tendency_report_summarizes_behavior(tmp_path):
    conn, now = _setup_db(tmp_path)
    scope = ("runtime_mt5_legacy_micro_capital", "legacy_micro_capital", "mt5", "mt5:demo:1")
    other_scope = ("runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1")

    conn.executemany(
        """
        INSERT INTO trades (
            id, trade_id, symbol, status, reason_code, profit_loss, r_multiple,
            market_regime, spread_entry, risk_reward, rsi_slope,
            is_london_session, is_newyork_session, open_time, close_time,
            evidence_stream, policy_mode, execution_mode, account_scope
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (1, "t_win", "EURUSD", "CLOSED_WIN", "ROUTED_TO_MT5", 12.0, 2.2, "TRENDING_BULL", 1.1, 2.2, 0.3, 1, 0, now, now, *scope),
            (2, "t_loss", "EURUSD", "CLOSED_LOSS", "ROUTED_TO_MT5", -10.0, -1.0, "TRENDING_BEAR", 2.4, 2.2, -0.2, 0, 1, now, now, *scope),
            (3, "t_reject", "GBPUSD", "REJECTED", "REJECTED_LOT", 0.0, 0.0, "TRENDING_BULL", 1.5, 2.2, 0.0, 1, 0, now, now, *scope),
            (5, "t_blocked", "AUDUSD", "REJECTED", "STRATEGIC_RISK_INELIGIBLE", 0.0, 0.0, "TRENDING_BULL", 1.0, 2.2, 0.1, 1, 0, now, now, *scope),
            (4, "t_other", "AUDUSD", "CLOSED_WIN", "ROUTED_TO_MT5", 20.0, 2.5, "TRENDING_BULL", 0.8, 2.2, 0.4, 1, 0, now, now, *other_scope),
        ],
    )
    conn.executemany(
        """
        INSERT INTO decision_funnel_events (
            id, decision_time, symbol, stage, outcome, reason_code, trade_id, details,
            evidence_stream, policy_mode, execution_mode, account_scope
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (1, now, "EURUSD", "SESSION", "PASS", "SESSION_ACTIVE", None, "", *scope),
            (2, now, "EURUSD", "REGIME", "PASS", "REGIME_TREND_BULL", None, "", *scope),
            (3, now, "EURUSD", "TECHNICAL", "PASS", "TECH_CONFIRMED_BUY", "t_win", "", *scope),
            (4, now, "EURUSD", "ML_RANKER", "BYPASS", "ML_RANKER_MODEL_UNAVAILABLE", "t_win", "bypass_untrained_model", *scope),
            (5, now, "EURUSD", "ROUTER", "ROUTED", "ROUTED_TO_MT5", "t_win", "", *scope),
            (6, now, "GBPUSD", "SESSION", "PASS", "SESSION_ACTIVE", None, "", *scope),
            (
                7,
                now,
                "GBPUSD",
                "REGIME",
                "REJECT",
                "REGIME_NO_TRADE",
                None,
                "direction_candidate=SELL adx=17.40 trend_state=FLAT",
                *scope,
            ),
            (8, now, "GBPUSD", "TECHNICAL", "SKIP", "TECH_SKIPPED_REGIME_REJECTED", None, "", *scope),
            (9, now, "AUDUSD", "SESSION", "PASS", "SESSION_ACTIVE", None, "", *scope),
            (10, now, "AUDUSD", "REGIME", "PASS", "REGIME_TREND_BULL", None, "", *scope),
            (
                11,
                now,
                "AUDUSD",
                "TECHNICAL",
                "REJECT",
                "TECH_PULLBACK_OR_RSI_INVALID",
                None,
                "direction=BUY pulled_back=False rsi_ok=True close=0.71672 rsi=55.68",
                *scope,
            ),
            (
                13,
                now,
                "AUDUSD",
                "TECHNICAL",
                "PASS",
                "TECH_CONFIRMED_BUY",
                "t_blocked",
                "direction=BUY stop_pips=9.84",
                *scope,
            ),
            (
                14,
                now,
                "AUDUSD",
                "STRATEGIC_RISK",
                "REJECT",
                "STRATEGIC_RISK_INELIGIBLE",
                "t_blocked",
                "minimum_risk_usd=0.9840 max_stop_pips_at_fixed_risk=5.00",
                *scope,
            ),
            (12, now, "AUDUSD", "SESSION", "PASS", "SESSION_ACTIVE", None, "", *other_scope),
        ],
    )
    conn.executemany(
        """
        INSERT INTO risk_events (
            id, timestamp, rule_name, severity, reason, trade_id,
            evidence_stream, policy_mode, execution_mode, account_scope
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (1, now, "ML_RANKER", "INFO", "bypass_untrained_model", "t_win", *scope),
            (2, now, "STATE_RECONCILIATION_FAILED", "BLOCK", "mismatch", None, *scope),
        ],
    )

    report = build_tendency_report(
        conn,
        filters=ScopeFilters(days=30, policy_mode="legacy_micro_capital", execution_mode="mt5"),
    )
    conn.close()

    assert report["summary"]["closed_trades"] == 2
    assert report["summary"]["wins"] == 1
    assert report["summary"]["late_lot_rejects"] == 1
    assert report["summary"]["deployability_blocks"] == 2
    assert report["summary"]["strategic_risk_rejects"] == 1
    assert report["summary"]["technical_passes"] == 2
    assert report["summary"]["routed_signals"] == 1
    assert report["summary"]["route_rate_from_technical_pass"] == 0.5

    hotspots = {(row["stage"], row["reason_code"], row["symbol"]): row["count"] for row in report["rejection_hotspots"]}
    assert hotspots[("REGIME", "REGIME_NO_TRADE", "GBPUSD")] == 1
    assert hotspots[("TECHNICAL", "TECH_PULLBACK_OR_RSI_INVALID", "AUDUSD")] == 1
    deployability = {
        (row["stage"], row["reason_code"], row["symbol"]): row["count"]
        for row in report["deployability_hotspots"]
    }
    assert deployability[("BROKER_RESULT", "REJECTED_LOT", "GBPUSD")] == 1
    assert deployability[("STRATEGIC_RISK", "STRATEGIC_RISK_INELIGIBLE", "AUDUSD")] == 1

    pressure = {row["symbol"]: row for row in report["symbol_pressure"]}
    assert pressure["EURUSD"]["technical_pass"] == 1
    assert pressure["EURUSD"]["routed"] == 1
    assert pressure["GBPUSD"]["regime_reject"] == 1
    assert pressure["GBPUSD"]["technical_skip"] == 1
    assert pressure["AUDUSD"]["technical_reject"] == 1
    assert pressure["AUDUSD"]["technical_pass"] == 1

    deployability_summary = {row["symbol"]: row for row in report["deployability_summary"]}
    assert deployability_summary["AUDUSD"]["strategic_risk_rejects"] == 1
    assert deployability_summary["AUDUSD"]["latest_minimum_risk_usd"] == 0.984
    assert deployability_summary["AUDUSD"]["latest_max_stop_pips_at_fixed_risk"] == 5.0
    assert deployability_summary["GBPUSD"]["broker_lot_rejects"] == 1

    reject_drivers = {
        (row["stage"], row["reason_code"], row["symbol"], row["driver"]): row["count"]
        for row in report["reject_drivers"]
    }
    assert reject_drivers[("TECHNICAL", "TECH_PULLBACK_OR_RSI_INVALID", "AUDUSD", "pullback_only")] == 1
    assert reject_drivers[("REGIME", "REGIME_NO_TRADE", "GBPUSD", "direction=SELL adx_band=16_to_20")] == 1

    assert report["feature_tendencies"]["wins"]["count"] == 1
    assert report["feature_tendencies"]["losses"]["count"] == 1
    assert report["risk_summary"][0]["rule_name"] in {"ML_RANKER", "STATE_RECONCILIATION_FAILED"}


def test_build_tendency_report_respects_account_scope(tmp_path):
    conn, now = _setup_db(tmp_path)
    conn.executemany(
        """
        INSERT INTO trades (
            id, trade_id, symbol, status, reason_code, profit_loss, r_multiple,
            market_regime, spread_entry, risk_reward, rsi_slope,
            is_london_session, is_newyork_session, open_time, close_time,
            evidence_stream, policy_mode, execution_mode, account_scope
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (1, "scope_live", "EURUSD", "CLOSED_WIN", "ROUTED_TO_MT5", 15.0, 2.0, "TRENDING_BULL", 1.1, 2.2, 0.2, 1, 0, now, now, "runtime_mt5_legacy_micro_capital", "legacy_micro_capital", "mt5", "mt5:demo:1"),
            (2, "scope_other", "EURUSD", "CLOSED_LOSS", "ROUTED_TO_MT5", -10.0, -1.0, "TRENDING_BEAR", 1.1, 2.2, -0.1, 1, 0, now, now, "runtime_mt5_legacy_micro_capital", "legacy_micro_capital", "mt5", "mt5:demo:2"),
        ],
    )

    report = build_tendency_report(
        conn,
        filters=ScopeFilters(
            days=30,
            policy_mode="legacy_micro_capital",
            execution_mode="mt5",
            account_scope="mt5:demo:1",
        ),
    )
    conn.close()

    assert report["summary"]["closed_trades"] == 1
    assert report["summary"]["wins"] == 1
    assert report["summary"]["losses"] == 0
