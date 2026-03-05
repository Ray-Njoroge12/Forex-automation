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
