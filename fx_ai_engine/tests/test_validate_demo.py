from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _setup_db(
    tmp_path: Path,
    trades: list[dict],
    risk_events: list[dict] | None = None,
    account_metrics: list[dict] | None = None,
) -> Path:
    """Create a minimal temp DB with synthetic closed trades."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT, symbol TEXT, direction TEXT,
            r_multiple REAL, profit_loss REAL,
            status TEXT, close_time TEXT, open_time TEXT, execution_time TEXT, reason_code TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE account_metrics (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, balance REAL, equity REAL, is_trading_halted INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            rule_name TEXT,
            severity TEXT,
            reason TEXT,
            trade_id TEXT
        )
    """)
    now = datetime.now(timezone.utc)
    for i, t in enumerate(trades):
        conn.execute(
            "INSERT INTO trades (trade_id, symbol, direction, r_multiple, "
            "profit_loss, status, close_time, open_time, execution_time, reason_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"AI_{i:04d}", t.get("symbol", "EURUSD"), t.get("direction", "BUY"),
                t.get("r_multiple", 0.0), t.get("profit_loss", 0.0),
                t.get("status", "CLOSED"),
                (now - timedelta(days=i % 25)).isoformat(),
                (now - timedelta(days=i % 25)).isoformat(),
                (now - timedelta(days=i % 25)).isoformat(),
                "TECH_PULLBACK_BUY",
            ),
        )
    for event in risk_events or []:
        conn.execute(
            "INSERT INTO risk_events (timestamp, rule_name, severity, reason, trade_id) VALUES (?,?,?,?,?)",
            (
                event.get("timestamp", now.isoformat()),
                event["rule_name"],
                event.get("severity", "BLOCK"),
                event.get("reason", event["rule_name"]),
                event.get("trade_id"),
            ),
        )
    for i, sample in enumerate(account_metrics or []):
        conn.execute(
            "INSERT INTO account_metrics (id, timestamp, balance, equity, is_trading_halted) VALUES (?,?,?,?,?)",
            (
                i + 1,
                sample.get("timestamp", now.isoformat()),
                sample.get("balance", 10.0),
                sample.get("equity", 10.0),
                int(sample.get("is_trading_halted", 0)),
            ),
        )
    conn.commit()
    conn.close()
    return db


def _make_trades(n: int, win_rate: float, avg_r_win: float = 2.5) -> list[dict]:
    wins = int(n * win_rate)
    return [
        {"r_multiple": avg_r_win, "status": "CLOSED"} if i < wins
        else {"r_multiple": -1.0, "status": "CLOSED"}
        for i in range(n)
    ]


def _make_account_metrics(total: int, *, halted_count: int = 0, balance: float = 10.0, equity: float = 10.0) -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {
            "timestamp": (now - timedelta(minutes=i)).isoformat(),
            "balance": balance,
            "equity": equity,
            "is_trading_halted": 1 if i < halted_count else 0,
        }
        for i in range(total)
    ]


# -- import after helper functions so monkeypatch can replace DB_PATH --
import validation.validate_demo as vd


def test_verdict_pending_insufficient_trades(tmp_path, monkeypatch) -> None:
    db = _setup_db(tmp_path, _make_trades(10, win_rate=0.6))
    monkeypatch.setattr(vd, "DB_PATH", db)
    verdict, metrics = vd.run_validation(days=30)
    assert verdict == "PENDING"
    assert metrics["total_trades"] == 10


def test_verdict_pass_all_criteria_met(tmp_path, monkeypatch) -> None:
    db = _setup_db(tmp_path, _make_trades(30, win_rate=0.9, avg_r_win=2.5))
    monkeypatch.setattr(vd, "DB_PATH", db)
    verdict, metrics = vd.run_validation(days=30)
    assert verdict == "PASS"
    assert metrics["win_rate"] >= 0.45
    assert metrics["avg_r"] >= 2.0


def test_verdict_abort_low_win_rate(tmp_path, monkeypatch) -> None:
    db = _setup_db(tmp_path, _make_trades(30, win_rate=0.35))  # 35% < 40% ABORT
    monkeypatch.setattr(vd, "DB_PATH", db)
    verdict, _ = vd.run_validation(days=30)
    assert verdict == "ABORT"


def test_verdict_abort_low_avg_r(tmp_path, monkeypatch) -> None:
    db = _setup_db(tmp_path, _make_trades(30, win_rate=0.5, avg_r_win=1.6))
    monkeypatch.setattr(vd, "DB_PATH", db)
    verdict, _ = vd.run_validation(days=30)
    assert verdict == "ABORT"


def test_verdict_warn_between_abort_and_pass(tmp_path, monkeypatch) -> None:
    # 42% WR: above ABORT (40%) but below PASS (45%) -> WARN
    db = _setup_db(tmp_path, _make_trades(30, win_rate=0.42, avg_r_win=7.5))
    monkeypatch.setattr(vd, "DB_PATH", db)
    verdict, _ = vd.run_validation(days=30)
    assert verdict == "WARN"


def test_max_drawdown_calculation() -> None:
    equity = [1000.0, 1100.0, 900.0, 950.0, 800.0]  # peak 1100, trough 800
    dd = vd._compute_max_drawdown(equity)
    expected = (1100.0 - 800.0) / 1100.0
    assert abs(dd - expected) < 0.001


def test_empty_db_returns_pending(tmp_path, monkeypatch) -> None:
    db = _setup_db(tmp_path, [])
    monkeypatch.setattr(vd, "DB_PATH", db)
    verdict, metrics = vd.run_validation(days=30)
    assert verdict == "PENDING"
    assert metrics["total_trades"] == 0


def test_validation_filters_partitioned_runtime_evidence(tmp_path, monkeypatch) -> None:
    db = tmp_path / "partitioned.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT, symbol TEXT, direction TEXT,
            r_multiple REAL, profit_loss REAL,
            status TEXT, close_time TEXT, open_time TEXT, execution_time TEXT, reason_code TEXT,
            evidence_stream TEXT, policy_mode TEXT, execution_mode TEXT, account_scope TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE account_metrics (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, balance REAL, equity REAL, is_trading_halted INTEGER DEFAULT 0,
            evidence_stream TEXT, policy_mode TEXT, execution_mode TEXT, account_scope TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    now = datetime.now(timezone.utc)
    conn.executemany(
        "INSERT INTO trades (trade_id, symbol, direction, r_multiple, profit_loss, status, close_time, open_time, execution_time, reason_code, evidence_stream, policy_mode, execution_mode, account_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("AI_core_1", "EURUSD", "BUY", 2.5, 25.0, "CLOSED_WIN", now.isoformat(), now.isoformat(), now.isoformat(), "TECH", "runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1"),
            ("AI_core_2", "EURUSD", "BUY", -1.0, -10.0, "CLOSED_LOSS", now.isoformat(), now.isoformat(), now.isoformat(), "TECH", "runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1"),
            ("AI_mock_1", "EURUSD", "BUY", 5.0, 50.0, "CLOSED_WIN", now.isoformat(), now.isoformat(), now.isoformat(), "TECH", "runtime_mock_core_srs", "core_srs", "mock", "mock"),
        ],
    )
    conn.executemany(
        "INSERT INTO account_metrics (id, timestamp, balance, equity, is_trading_halted, evidence_stream, policy_mode, execution_mode, account_scope) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, now.isoformat(), 100000.0, 100000.0, 0, "runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1"),
            (2, now.isoformat(), 100000.0, 99000.0, 0, "runtime_mt5_core_srs", "core_srs", "mt5", "mt5:demo:1"),
            (3, now.isoformat(), 10000.0, 10000.0, 0, "runtime_mock_core_srs", "core_srs", "mock", "mock"),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="core_srs")

    assert verdict == "PENDING"
    assert metrics["total_trades"] == 2
    assert metrics["max_drawdown"] == pytest.approx(0.01)


# --- check_abort_criteria tests ---

def test_compounding_milestone_at_10():
    """At $10, milestone should recommend $0.50 fixed risk."""
    from config_microcapital import get_config_for_balance
    config = get_config_for_balance(10.0)
    assert config["FIXED_RISK_USD"] == 0.50
    assert config["MAX_SIMULTANEOUS_TRADES"] == 1


def test_compounding_milestone_at_25():
    """At $25, milestone should step up to $0.75 fixed risk."""
    from config_microcapital import get_config_for_balance
    config = get_config_for_balance(25.0)
    assert config["FIXED_RISK_USD"] == 0.75


def test_compounding_milestone_at_100():
    """At $100, milestone should step up to $3.00 fixed risk, 2 trades."""
    from config_microcapital import get_config_for_balance
    config = get_config_for_balance(100.0)
    assert config["FIXED_RISK_USD"] == 3.00
    assert config["MAX_SIMULTANEOUS_TRADES"] == 2


def test_abort_criteria_triggers_on_high_drawdown():
    """check_abort_criteria must flag abort when drawdown > 20%."""
    from validation.validate_demo import check_abort_criteria
    result = check_abort_criteria(drawdown_pct=0.21, win_rate=0.50, avg_r=2.0, total_trades=10)
    assert result["abort"] is True
    assert "drawdown" in result["reason"].lower()


def test_abort_criteria_triggers_on_low_win_rate():
    """check_abort_criteria must flag abort when win rate < 40% after 25 trades."""
    from validation.validate_demo import check_abort_criteria
    result = check_abort_criteria(drawdown_pct=0.10, win_rate=0.38, avg_r=2.0, total_trades=25)
    assert result["abort"] is True
    assert "win rate" in result["reason"].lower()


def test_no_abort_when_criteria_met():
    """check_abort_criteria must not abort when all criteria are good."""
    from validation.validate_demo import check_abort_criteria
    result = check_abort_criteria(drawdown_pct=0.10, win_rate=0.50, avg_r=2.2, total_trades=30)
    assert result["abort"] is False


def test_preserve_10_wave_a_passes_clean_safety_gate(tmp_path, monkeypatch) -> None:
    db = _setup_db(tmp_path, _make_trades(2, win_rate=0.5, avg_r_win=2.5))
    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="preserve_10_wave_a")

    assert verdict == "PASS"
    assert metrics["late_lot_rejections"] == 0
    assert metrics["stale_state_failures"] == 0
    assert metrics["reconciliation_failures"] == 0


def test_preserve_10_wave_a_warns_on_late_lot_reject(tmp_path, monkeypatch) -> None:
    db = _setup_db(
        tmp_path,
        [],
        risk_events=[],
    )
    conn = sqlite3.connect(db)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO trades (trade_id, symbol, direction, r_multiple, profit_loss, status, close_time, open_time, execution_time, reason_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("AI_REJECTED_LOT", "EURUSD", "BUY", 0.0, 0.0, "REJECTED_LOT", now, now, now, "REJECTED_LOT"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="preserve_10_wave_a")

    assert verdict == "WARN"
    assert metrics["late_lot_rejections"] == 1


def test_preserve_10_wave_a_warns_on_state_authority_failures(tmp_path, monkeypatch) -> None:
    db = _setup_db(
        tmp_path,
        _make_trades(1, win_rate=1.0, avg_r_win=2.5),
        risk_events=[
            {"rule_name": "STATE_STALE"},
            {"rule_name": "STATE_RECONCILIATION_FAILED"},
        ],
    )
    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="preserve_10_wave_a")

    assert verdict == "WARN"
    assert metrics["stale_state_failures"] == 1
    assert metrics["reconciliation_failures"] == 1


def test_preserve_10_wave_a_pending_without_observed_evidence(tmp_path, monkeypatch) -> None:
    db = _setup_db(tmp_path, [], risk_events=[])
    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="preserve_10_wave_a")

    assert verdict == "PENDING"
    assert metrics["observed_gate_events"] == 0


def test_preserve_10_operational_passes_with_clean_startup_feasibility_and_halt_evidence(tmp_path, monkeypatch) -> None:
    db = _setup_db(
        tmp_path,
        _make_trades(2, win_rate=0.5, avg_r_win=2.5),
        risk_events=[
            {
                "rule_name": "PRESERVE_10_STARTUP_APPROVAL",
                "severity": "INFO",
                "reason": "PRESERVE_10_STARTUP_APPROVED facts ready",
            }
        ],
        account_metrics=_make_account_metrics(3),
    )
    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="preserve_10_operational")

    assert verdict == "PASS"
    assert metrics["startup_approval_failures"] == 0
    assert metrics["preroute_rejections"] == 0
    assert metrics["preroute_rejection_rate"] == 0.0
    assert metrics["restart_risk_anomalies"] == 0
    assert metrics["halted_account_samples"] == 0
    assert metrics["account_metric_samples"] == 3


def test_preserve_10_operational_warns_on_startup_failures_and_preroute_rejection_rate(tmp_path, monkeypatch, capsys) -> None:
    db = _setup_db(
        tmp_path,
        [],
        risk_events=[
            {
                "rule_name": "PRESERVE_10_STARTUP_APPROVAL",
                "severity": "BLOCK",
                "reason": "APPROVAL_ACCOUNT_INFO_MISSING startup approval unavailable",
            },
            {
                "rule_name": "PRE_ROUTE_FEASIBILITY",
                "severity": "WARN",
                "reason": "mode=preserve_10 raw_limit=0.002500 min_lot=0.0100",
            },
        ],
        account_metrics=_make_account_metrics(2),
    )
    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="preserve_10_operational")

    assert verdict == "WARN"
    assert metrics["startup_approval_failures"] == 1
    assert metrics["startup_approval_observations"] == 1
    assert metrics["preroute_rejections"] == 1
    assert metrics["feasibility_observations"] == 1
    assert metrics["preroute_rejection_rate"] == 1.0
    out = capsys.readouterr().out
    assert "Startup Approval Refusals" in out
    assert "Pre-route Refusals" in out
    assert "operators saw Preserve-$10 startup approval fail closed" in out
    assert "Evidence scope: Preserve-$10 operational proof only" in out


def test_preserve_10_operational_warns_on_restart_anomalies_and_halt_behavior(tmp_path, monkeypatch) -> None:
    db = _setup_db(
        tmp_path,
        _make_trades(1, win_rate=1.0, avg_r_win=2.5),
        risk_events=[
            {
                "rule_name": "PRESERVE_10_STARTUP_APPROVAL",
                "severity": "INFO",
                "reason": "PRESERVE_10_STARTUP_APPROVED facts ready",
            },
            {"rule_name": "STATE_STALE"},
            {"rule_name": "STATE_RECONCILIATION_FAILED"},
        ],
        account_metrics=_make_account_metrics(4, halted_count=2),
    )
    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="preserve_10_operational")

    assert verdict == "WARN"
    assert metrics["restart_risk_anomalies"] == 2
    assert metrics["halted_account_samples"] == 2
    assert metrics["halted_sample_rate"] == 0.5


def test_preserve_10_operational_aborts_on_drawdown_floor_breach(tmp_path, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    db = _setup_db(
        tmp_path,
        _make_trades(2, win_rate=0.5, avg_r_win=2.5),
        risk_events=[
            {
                "rule_name": "PRESERVE_10_STARTUP_APPROVAL",
                "severity": "INFO",
                "reason": "PRESERVE_10_STARTUP_APPROVED facts ready",
            }
        ],
        account_metrics=[
            {"timestamp": (now - timedelta(minutes=1)).isoformat(), "balance": 10.0, "equity": 10.0, "is_trading_halted": 0},
            {"timestamp": now.isoformat(), "balance": 10.0, "equity": 7.0, "is_trading_halted": 0},
        ],
    )
    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="preserve_10_operational")

    assert verdict == "ABORT"
    assert metrics["max_drawdown"] == 0.3


def test_preserve_10_operational_pending_without_full_operational_evidence(tmp_path, monkeypatch) -> None:
    db = _setup_db(
        tmp_path,
        _make_trades(1, win_rate=1.0, avg_r_win=2.5),
        risk_events=[],
        account_metrics=[],
    )
    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="preserve_10_operational")

    assert verdict == "PENDING"
    assert metrics["startup_approval_observations"] == 0
    assert metrics["account_metric_samples"] == 0


def test_core_srs_profile_remains_isolated_from_preserve_10_operational_evidence(tmp_path, monkeypatch, capsys) -> None:
    db = _setup_db(
        tmp_path,
        _make_trades(30, win_rate=0.9, avg_r_win=2.5),
        risk_events=[
            {
                "rule_name": "PRESERVE_10_STARTUP_APPROVAL",
                "severity": "BLOCK",
                "reason": "APPROVAL_ACCOUNT_INFO_MISSING startup approval unavailable",
            },
            {
                "rule_name": "PRE_ROUTE_FEASIBILITY",
                "severity": "WARN",
                "reason": "mode=preserve_10 raw_limit=0.002500 min_lot=0.0100",
            },
            {"rule_name": "STATE_STALE"},
        ],
        account_metrics=_make_account_metrics(3, halted_count=3),
    )
    monkeypatch.setattr(vd, "DB_PATH", db)

    verdict, metrics = vd.run_validation(days=30, profile="core_srs")

    assert verdict == "PASS"
    assert "startup_approval_failures" not in metrics
    assert "preroute_rejections" not in metrics
    assert "halted_account_samples" not in metrics
    out = capsys.readouterr().out
    assert "Evidence Label: Core SRS v1" in out
    assert "Preserve-$10 Operator Checks" not in out
    assert "Preserve-$10 doctrine and operational diagnostics are reported separately" in out
