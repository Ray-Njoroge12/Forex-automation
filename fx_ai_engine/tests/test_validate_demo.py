from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _setup_db(tmp_path: Path, trades: list[dict]) -> Path:
    """Create a minimal temp DB with synthetic closed trades."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT, symbol TEXT, direction TEXT,
            r_multiple REAL, profit_loss REAL,
            status TEXT, close_time TEXT, reason_code TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE account_metrics (
            id INTEGER PRIMARY KEY,
            timestamp TEXT, balance REAL, equity REAL
        )
    """)
    now = datetime.now(timezone.utc)
    for i, t in enumerate(trades):
        conn.execute(
            "INSERT INTO trades (trade_id, symbol, direction, r_multiple, "
            "profit_loss, status, close_time, reason_code) VALUES (?,?,?,?,?,?,?,?)",
            (
                f"AI_{i:04d}", t.get("symbol", "EURUSD"), t.get("direction", "BUY"),
                t.get("r_multiple", 0.0), t.get("profit_loss", 0.0),
                t.get("status", "CLOSED"),
                (now - timedelta(days=i % 25)).isoformat(),
                "TECH_PULLBACK_BUY",
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


# -- import after helper functions so monkeypatch can replace DB_PATH --
import validation.validate_demo as vd


def test_verdict_pending_insufficient_trades(tmp_path, monkeypatch) -> None:
    db = _setup_db(tmp_path, _make_trades(10, win_rate=0.6))
    monkeypatch.setattr(vd, "DB_PATH", db)
    verdict, metrics = vd.run_validation(days=30)
    assert verdict == "PENDING"
    assert metrics["total_trades"] == 10


def test_verdict_pass_all_criteria_met(tmp_path, monkeypatch) -> None:
    db = _setup_db(tmp_path, _make_trades(30, win_rate=0.5, avg_r_win=2.5))
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
    db = _setup_db(tmp_path, _make_trades(30, win_rate=0.42, avg_r_win=2.2))
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
