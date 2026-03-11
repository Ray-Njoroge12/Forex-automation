from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit_rejected_setups import (
    CounterfactualCandidate,
    ScopeFilters,
    build_rejected_setup_audit,
    evaluate_candidate_on_bars,
    extract_counterfactual_candidates,
)


def _setup_db(tmp_path):
    db = tmp_path / "counterfactual.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            trade_id TEXT,
            symbol TEXT,
            direction TEXT,
            stop_loss REAL,
            take_profit REAL,
            entry_price REAL,
            status TEXT,
            reason_code TEXT,
            open_time TEXT,
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
    return conn


def _bars(rows):
    frame = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame.set_index("time")


def test_build_rejected_setup_audit_tracks_exact_approximate_and_gap_cases(tmp_path):
    conn = _setup_db(tmp_path)
    scope = ("runtime_mt5_legacy_micro_capital", "legacy_micro_capital", "mt5", "mt5:demo:1")
    t0 = datetime(2026, 3, 11, 10, 30, tzinfo=timezone.utc)
    t1 = datetime(2026, 3, 11, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc)

    conn.executemany(
        """
        INSERT INTO trades (
            id, trade_id, symbol, direction, stop_loss, take_profit, entry_price,
            status, reason_code, open_time, evidence_stream, policy_mode, execution_mode, account_scope
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (1, "t_ml", "GBPUSD", "SELL", 10.0, 20.0, 0.0, "REJECTED", "ML_RANKER_LOW_PROB", t1.isoformat(), *scope),
            (2, "t_lot", "AUDUSD", "BUY", 10.0, 20.0, 0.0, "REJECTED", "REJECTED_LOT", t2.isoformat(), *scope),
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
            (
                1,
                t0.isoformat(),
                "EURUSD",
                "TECHNICAL",
                "REJECT",
                "TECH_PULLBACK_OR_RSI_INVALID",
                None,
                "direction=BUY close=1.10000 pulled_back=False rsi_ok=True",
                *scope,
            ),
            (
                2,
                t1.isoformat(),
                "GBPUSD",
                "ML_RANKER",
                "REJECT",
                "ML_RANKER_LOW_PROB",
                "t_ml",
                "prob=0.42 threshold=0.50",
                *scope,
            ),
            (
                3,
                t2.isoformat(),
                "USDJPY",
                "REGIME",
                "REJECT",
                "REGIME_NO_TRADE",
                None,
                "regime=NO_TRADE trend_state=FLAT direction_candidate=SELL close=148.50000 ema_fast=148.40000 ema_slow=148.60000",
                *scope,
            ),
            (
                4,
                t2.isoformat(),
                "USDJPY",
                "TECHNICAL",
                "SKIP",
                "TECH_SKIPPED_REGIME_REJECTED",
                None,
                "regime=NO_TRADE regime_reason=REGIME_NO_TRADE trend_state=FLAT",
                *scope,
            ),
        ],
    )

    report = build_rejected_setup_audit(
        conn,
        filters=ScopeFilters(days=30, policy_mode="legacy_micro_capital", execution_mode="mt5"),
        bars_by_symbol={
            "EURUSD": _bars(
                [
                    ("2026-03-11T10:30:00+00:00", 1.1000, 1.1012, 1.0997, 1.1008),
                    ("2026-03-11T10:45:00+00:00", 1.1008, 1.1014, 1.1002, 1.1010),
                ]
            ),
            "GBPUSD": _bars(
                [
                    ("2026-03-11T09:00:00+00:00", 1.2500, 1.2504, 1.2479, 1.2482),
                    ("2026-03-11T09:15:00+00:00", 1.2482, 1.2484, 1.2475, 1.2478),
                ]
            ),
            "AUDUSD": _bars(
                [
                    ("2026-03-11T08:00:00+00:00", 0.6600, 0.6603, 0.6588, 0.6591),
                    ("2026-03-11T08:15:00+00:00", 0.6591, 0.6593, 0.6585, 0.6589),
                ]
            ),
            "USDJPY": _bars(
                [
                    ("2026-03-11T08:00:00+00:00", 148.50, 148.52, 148.30, 148.34),
                    ("2026-03-11T08:15:00+00:00", 148.34, 148.36, 148.20, 148.24),
                ]
            ),
        },
        horizon_bars=4,
        threshold_pips=10.0,
    )
    conn.close()

    summary = report["summary"]
    assert summary["total_reject_like_events"] == 4
    assert summary["audited_candidates"] == 4
    assert summary["exact_reference_candidates"] == 2
    assert summary["approximate_reference_candidates"] == 2
    assert summary["telemetry_gap_events"] == 0
    assert summary["threshold_hit_candidates"] == 2

    outcomes = {row["outcome_label"]: row["count"] for row in report["outcome_summary"]}
    assert outcomes["EXCURSION_ONLY"] == 2
    assert outcomes["TARGET_FIRST"] == 1
    assert outcomes["STOP_FIRST"] == 1

    hotspots = {
        (row["stage"], row["reason_code"], row["symbol"]): row
        for row in report["reason_hotspots"]
    }
    assert hotspots[("ML_RANKER", "ML_RANKER_LOW_PROB", "GBPUSD")]["target_first_rate"] == 1.0
    assert hotspots[("BROKER_RESULT", "REJECTED_LOT", "AUDUSD")]["approximate_reference_share"] == 1.0
    assert hotspots[("REGIME", "REGIME_NO_TRADE", "USDJPY")]["threshold_hit_rate"] == 1.0
    assert report["telemetry_gaps"] == []


def test_evaluate_candidate_on_bars_marks_same_bar_stop_and_target_as_ambiguous():
    candidate = CounterfactualCandidate(
        decision_time=datetime(2026, 3, 11, 10, 30, tzinfo=timezone.utc),
        symbol="EURUSD",
        stage="BROKER_RESULT",
        reason_code="REJECTED_LOT",
        trade_id="ambiguous",
        direction="BUY",
        details="status=REJECTED",
        reference_price=1.1000,
        reference_mode="exact_details_close",
        stop_pips=10.0,
        target_pips=10.0,
    )
    bars = _bars(
        [
            ("2026-03-11T10:30:00+00:00", 1.1000, 1.1012, 1.0988, 1.1005),
        ]
    )

    result = evaluate_candidate_on_bars(candidate, bars, horizon_bars=2, threshold_pips=10.0)

    assert result.outcome_label == "AMBIGUOUS_BOTH_HIT_SAME_BAR"
    assert result.mfe_pips == 12.0
    assert result.mae_pips == 12.0


def test_extract_counterfactual_candidates_counts_regime_not_trending_without_price_context_as_gap(tmp_path):
    conn = _setup_db(tmp_path)
    scope = ("runtime_mt5_legacy_micro_capital", "legacy_micro_capital", "mt5", "mt5:demo:1")
    t0 = datetime(2026, 3, 11, 10, 30, tzinfo=timezone.utc)
    conn.execute(
        """
        INSERT INTO decision_funnel_events (
            id, decision_time, symbol, stage, outcome, reason_code, trade_id, details,
            evidence_stream, policy_mode, execution_mode, account_scope
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            1,
            t0.isoformat(),
            "EURUSD",
            "TECHNICAL",
            "REJECT",
            "TECH_REGIME_NOT_TRENDING",
            None,
            "regime=NO_TRADE regime_reason=REGIME_NO_TRADE trend_state=FLAT",
            *scope,
        ),
    )

    extracted = extract_counterfactual_candidates(
        conn,
        filters=ScopeFilters(days=30, policy_mode="legacy_micro_capital", execution_mode="mt5"),
    )
    conn.close()

    assert extracted["total_reject_like_events"] == 1
    assert extracted["total_telemetry_gap_events"] == 1
    assert extracted["candidates"] == []
    assert extracted["telemetry_gaps"][0]["reason_code"] == "TECH_REGIME_NOT_TRENDING"


def test_extract_counterfactual_candidates_audits_regime_not_trending_when_reference_context_exists(tmp_path):
    conn = _setup_db(tmp_path)
    scope = ("runtime_mt5_legacy_micro_capital", "legacy_micro_capital", "mt5", "mt5:demo:1")
    t0 = datetime(2026, 3, 11, 10, 30, tzinfo=timezone.utc)
    conn.execute(
        """
        INSERT INTO decision_funnel_events (
            id, decision_time, symbol, stage, outcome, reason_code, trade_id, details,
            evidence_stream, policy_mode, execution_mode, account_scope
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            1,
            t0.isoformat(),
            "EURUSD",
            "TECHNICAL",
            "REJECT",
            "TECH_REGIME_NOT_TRENDING",
            None,
            "direction=BUY close=1.10000 regime=NO_TRADE regime_reason=REGIME_NO_TRADE trend_state=FLAT",
            *scope,
        ),
    )

    extracted = extract_counterfactual_candidates(
        conn,
        filters=ScopeFilters(days=30, policy_mode="legacy_micro_capital", execution_mode="mt5"),
    )
    conn.close()

    assert extracted["total_reject_like_events"] == 1
    assert extracted["total_telemetry_gap_events"] == 0
    assert len(extracted["candidates"]) == 1
    candidate = extracted["candidates"][0]
    assert candidate.reason_code == "TECH_REGIME_NOT_TRENDING"
    assert candidate.direction == "BUY"
    assert candidate.reference_price == 1.1
