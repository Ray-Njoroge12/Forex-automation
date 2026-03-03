from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from core.account_status import AccountStatus
from core.types import TechnicalSignal

DB_PATH = Path("database/trading_state.db")
SCHEMA_PATH = Path("database/schema.sql")


@contextmanager
def get_conn(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_schema() -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn() as conn:
        conn.executescript(schema)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_phase8_columns() -> None:
    """
    Adds Phase 8 columns for users that initialized earlier baseline schema.
    SQLite CREATE TABLE IF NOT EXISTS does not backfill new columns.
    """
    with get_conn() as conn:
        _ensure_column(conn, "trades", "trade_id", "TEXT")
        _ensure_column(conn, "trades", "direction", "TEXT")
        _ensure_column(conn, "trades", "risk_percent", "REAL")
        _ensure_column(conn, "trades", "reason_code", "TEXT")
        _ensure_column(conn, "trades", "spread_entry", "REAL")
        _ensure_column(conn, "trades", "slippage", "REAL")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_trade_id ON trades(trade_id)"
        )

        _ensure_column(conn, "account_metrics", "open_risk_percent", "REAL DEFAULT 0.0")
        _ensure_column(conn, "account_metrics", "open_usd_exposure_count", "INTEGER DEFAULT 0")
        _ensure_column(conn, "account_metrics", "drawdown_percent", "REAL DEFAULT 0.0")


def migrate_add_risk_events() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS risk_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                rule_name TEXT NOT NULL,
                severity TEXT NOT NULL,
                reason TEXT NOT NULL,
                trade_id TEXT
            )
            """
        )


def migrate_add_ml_feature_columns() -> None:
    """Adds ML feature columns required for SignalRanker training. Idempotent."""
    with get_conn() as conn:
        _ensure_column(conn, "trades", "regime_confidence", "REAL")
        _ensure_column(conn, "trades", "rsi_at_entry", "REAL")
        _ensure_column(conn, "trades", "atr_ratio", "REAL")
        _ensure_column(conn, "trades", "is_london_session", "INTEGER")
        _ensure_column(conn, "trades", "is_newyork_session", "INTEGER")
        _ensure_column(conn, "trades", "rate_differential", "REAL")
        _ensure_column(conn, "trades", "risk_reward", "REAL")


def insert_trade_proposal(
    signal: TechnicalSignal,
    status: str,
    reason_code: str,
    risk_percent: float,
    market_regime: str,
    *,
    regime_confidence: float = 0.0,
    atr_ratio: float = 1.0,
    is_london_session: int = 0,
    is_newyork_session: int = 0,
    rate_differential: float = 0.0,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO trades (
                trade_id, symbol, direction, stop_loss, take_profit,
                risk_percent, market_regime, status, reason_code, open_time,
                regime_confidence, rsi_at_entry, atr_ratio,
                is_london_session, is_newyork_session, rate_differential, risk_reward
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.trade_id,
                signal.symbol,
                signal.direction,
                signal.stop_pips,
                signal.take_profit_pips,
                risk_percent,
                market_regime,
                status,
                reason_code,
                datetime.now(timezone.utc).isoformat(),
                regime_confidence,
                signal.rsi_at_entry,
                atr_ratio,
                is_london_session,
                is_newyork_session,
                rate_differential,
                signal.risk_reward,
            ),
        )


def update_trade_execution_result(payload: dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE trades
               SET trade_ticket = ?,
                   status = ?,
                   entry_price = ?,
                   slippage = ?,
                   spread_entry = ?,
                   profit_loss = ?,
                   r_multiple = ?,
                   close_time = ?
             WHERE trade_id = ?
            """,
            (
                payload.get("ticket"),
                payload.get("status", "UNKNOWN"),
                payload.get("entry_price", 0.0),
                payload.get("slippage", 0.0),
                payload.get("spread_at_entry", 0.0),
                payload.get("profit_loss", 0.0),
                payload.get("r_multiple", 0.0),
                payload.get("close_time", datetime.now(timezone.utc).isoformat()),
                payload.get("trade_id"),
            ),
        )


def insert_account_metrics(account_status: AccountStatus) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO account_metrics (
                timestamp, balance, equity, open_risk_percent,
                open_usd_exposure_count, daily_loss_percent, weekly_loss_percent,
                drawdown_percent, consecutive_losses, is_trading_halted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_status.updated_at.isoformat(),
                account_status.balance,
                account_status.equity,
                account_status.open_risk_percent,
                account_status.open_usd_exposure_count,
                account_status.daily_loss_percent,
                account_status.weekly_loss_percent,
                account_status.drawdown_percent,
                account_status.consecutive_losses,
                int(account_status.is_trading_halted),
            ),
        )


def insert_risk_event(rule_name: str, severity: str, reason: str, trade_id: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO risk_events (timestamp, rule_name, severity, reason, trade_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (datetime.now(timezone.utc).isoformat(), rule_name, severity, reason, trade_id),
        )


def get_recent_r_multiples(limit: int = 100) -> list[float]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r_multiple
              FROM trades
             WHERE status IN ('EXECUTED', 'CLOSED')
          ORDER BY close_time DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [float(row["r_multiple"]) for row in rows if row["r_multiple"] is not None]
