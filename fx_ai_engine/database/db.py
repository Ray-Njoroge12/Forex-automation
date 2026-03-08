from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from core.account_status import AccountStatus
from core.evidence import (
    EvidenceContext,
    LEGACY_UNPARTITIONED_ACCOUNT_SCOPE,
    LEGACY_UNPARTITIONED_STREAM,
)
from core.types import TechnicalSignal

DB_PATH = Path(__file__).parent / "trading_state.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


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


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


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
        _ensure_column(conn, "trades", "position_ticket", "INTEGER")
        _ensure_column(conn, "trades", "direction", "TEXT")
        _ensure_column(conn, "trades", "risk_percent", "REAL")
        _ensure_column(conn, "trades", "reason_code", "TEXT")
        _ensure_column(conn, "trades", "spread_entry", "REAL")
        _ensure_column(conn, "trades", "spread_signal_pips", "REAL")
        _ensure_column(conn, "trades", "spread_exec_price", "REAL")
        _ensure_column(conn, "trades", "slippage", "REAL")
        _ensure_column(conn, "trades", "execution_time", "DATETIME")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_trade_id ON trades(trade_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_position_ticket ON trades(position_ticket)"
        )

        _ensure_column(conn, "account_metrics", "open_risk_percent", "REAL DEFAULT 0.0")
        _ensure_column(conn, "account_metrics", "open_usd_exposure_count", "INTEGER DEFAULT 0")
        _ensure_column(conn, "account_metrics", "drawdown_percent", "REAL DEFAULT 0.0")


def migrate_add_restart_state_columns() -> None:
    with get_conn() as conn:
        _ensure_column(conn, "account_metrics", "peak_equity", "REAL DEFAULT 0.0")
        _ensure_column(conn, "account_metrics", "daily_anchor_date", "TEXT DEFAULT ''")
        _ensure_column(conn, "account_metrics", "daily_anchor_equity", "REAL DEFAULT 0.0")
        _ensure_column(conn, "account_metrics", "weekly_anchor_key", "TEXT DEFAULT ''")
        _ensure_column(conn, "account_metrics", "weekly_anchor_equity", "REAL DEFAULT 0.0")


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


def migrate_add_decision_funnel_events() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_funnel_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                evidence_stream TEXT DEFAULT 'legacy_unpartitioned',
                policy_mode TEXT DEFAULT 'legacy_unpartitioned',
                execution_mode TEXT DEFAULT 'legacy',
                account_scope TEXT DEFAULT 'legacy_unpartitioned',
                decision_time DATETIME NOT NULL,
                symbol TEXT,
                stage TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                details TEXT DEFAULT '',
                trade_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_funnel_scope_stage_time
                ON decision_funnel_events(evidence_stream, account_scope, stage, decision_time)
            """
        )


def _default_evidence_context() -> EvidenceContext:
    return EvidenceContext(
        evidence_stream=LEGACY_UNPARTITIONED_STREAM,
        policy_mode=LEGACY_UNPARTITIONED_STREAM,
        execution_mode="legacy",
        account_scope=LEGACY_UNPARTITIONED_ACCOUNT_SCOPE,
    )


def migrate_add_evidence_partition_columns() -> None:
    with get_conn() as conn:
        for table in ("trades", "account_metrics", "risk_events", "decision_funnel_events"):
            if not _table_exists(conn, table):
                continue
            _ensure_column(conn, table, "evidence_stream", "TEXT DEFAULT 'legacy_unpartitioned'")
            _ensure_column(conn, table, "policy_mode", "TEXT DEFAULT 'legacy_unpartitioned'")
            _ensure_column(conn, table, "execution_mode", "TEXT DEFAULT 'legacy'")
            _ensure_column(conn, table, "account_scope", "TEXT DEFAULT 'legacy_unpartitioned'")
            conn.execute(
                f"""
                UPDATE {table}
                   SET evidence_stream = COALESCE(NULLIF(TRIM(evidence_stream), ''), ?),
                       policy_mode = COALESCE(NULLIF(TRIM(policy_mode), ''), ?),
                       execution_mode = COALESCE(NULLIF(TRIM(execution_mode), ''), 'legacy'),
                       account_scope = COALESCE(NULLIF(TRIM(account_scope), ''), ?)
                 WHERE COALESCE(NULLIF(TRIM(evidence_stream), ''), '') = ''
                    OR COALESCE(NULLIF(TRIM(policy_mode), ''), '') = ''
                    OR COALESCE(NULLIF(TRIM(execution_mode), ''), '') = ''
                    OR COALESCE(NULLIF(TRIM(account_scope), ''), '') = ''
                """,
                (
                    LEGACY_UNPARTITIONED_STREAM,
                    LEGACY_UNPARTITIONED_STREAM,
                    LEGACY_UNPARTITIONED_ACCOUNT_SCOPE,
                ),
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
        _ensure_column(conn, "trades", "rsi_slope", "REAL")


def insert_trade_proposal(
    signal: TechnicalSignal,
    status: str,
    reason_code: str,
    risk_percent: float,
    market_regime: str,
    *,
    evidence_context: EvidenceContext | None = None,
    regime_confidence: float = 0.0,
    atr_ratio: float = 1.0,
    is_london_session: int = 0,
    is_newyork_session: int = 0,
    rate_differential: float = 0.0,
) -> None:
    evidence = evidence_context or _default_evidence_context()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO trades (
                trade_id, evidence_stream, policy_mode, execution_mode, account_scope,
                symbol, direction, order_type, lot_size, 
                stop_loss, take_profit,
                risk_percent, market_regime, status, reason_code, open_time,
                regime_confidence, rsi_at_entry, atr_ratio,
                is_london_session, is_newyork_session, rate_differential, risk_reward, rsi_slope,
                spread_entry, spread_signal_pips
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.trade_id,
                evidence.evidence_stream,
                evidence.policy_mode,
                evidence.execution_mode,
                evidence.account_scope,
                signal.symbol,
                signal.direction,
                "MARKET",  # order_type
                0.0,       # lot_size (placeholder until execution)
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
                signal.rsi_slope,
                signal.spread_entry,
                signal.spread_entry,
            ),
        )


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def update_trade_execution_result(payload: dict[str, Any]) -> None:
    raw_status = str(payload.get("status", "UNKNOWN")).upper()
    ticket = _positive_int(payload.get("ticket")) or 0
    position_ticket = _positive_int(payload.get("position_ticket"))
    executed_open = raw_status == "EXECUTED" and ticket > 0
    rejected = raw_status.startswith("REJECTED")
    status = "EXECUTED_OPEN" if executed_open else ("REJECTED" if rejected else raw_status)
    reason_code = raw_status

    with get_conn() as conn:
        if executed_open:
            conn.execute(
                """
                UPDATE trades
                   SET trade_ticket = ?,
                       position_ticket = ?,
                       status = ?,
                       entry_price = ?,
                       slippage = ?,
                       spread_exec_price = ?,
                       execution_time = ?
                 WHERE trade_id = ?
                """,
                (
                    ticket,
                    position_ticket or ticket,
                    status,
                    payload.get("entry_price", 0.0),
                    payload.get("slippage", 0.0),
                    payload.get("spread_at_entry", 0.0),
                    payload.get("close_time", datetime.now(timezone.utc).isoformat()),
                    payload.get("trade_id"),
                ),
            )
            return

        close_time = payload.get("close_time", datetime.now(timezone.utc).isoformat())
        conn.execute(
            """
            UPDATE trades
               SET trade_ticket = ?,
                   position_ticket = COALESCE(position_ticket, ?),
                   status = ?,
                   reason_code = ?,
                   entry_price = ?,
                   slippage = ?,
                   spread_exec_price = ?,
                   close_time = ?
             WHERE trade_id = ?
            """,
            (
                ticket if ticket > 0 else None,
                position_ticket or ticket,
                status,
                reason_code,
                payload.get("entry_price", 0.0),
                payload.get("slippage", 0.0),
                payload.get("spread_at_entry", 0.0),
                close_time,
                payload.get("trade_id"),
            ),
        )


def mark_trade_execution_uncertain(
    trade_id: str,
    reason_code: str = "PRESERVE_10_EXECUTION_UNCERTAIN",
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE trades
               SET status = ?,
                   reason_code = ?
             WHERE trade_id = ?
               AND status IN ('PENDING', 'EXECUTION_UNCERTAIN')
            """,
            ("EXECUTION_UNCERTAIN", reason_code, trade_id),
        )


def update_trade_exit_result(
    payload: dict[str, Any],
    *,
    evidence_context: EvidenceContext | None = None,
) -> bool:
    if payload.get("is_final_exit") is False:
        return False

    pnl = float(payload.get("profit_loss", 0.0))
    raw_status = str(payload.get("status", "CLOSED")).upper()
    if raw_status.startswith("CLOSED"):
        status = raw_status
    elif pnl > 0:
        status = "CLOSED_WIN"
    elif pnl < 0:
        status = "CLOSED_LOSS"
    else:
        status = "CLOSED_BREAKEVEN"

    close_time = payload.get("close_time", datetime.now(timezone.utc).isoformat())
    r_multiple = payload.get("r_multiple")
    position_ticket = _positive_int(payload.get("position_ticket"))
    ticket = _positive_int(payload.get("ticket"))
    trade_id = str(payload.get("trade_id") or "").strip() or None

    filter_clause = ""
    filter_params: tuple[Any, ...] = ()
    if evidence_context is not None:
        filter_clause = " AND evidence_stream = ? AND account_scope = ?"
        filter_params = (evidence_context.evidence_stream, evidence_context.account_scope)

    def _apply_update(conn: sqlite3.Connection, where_clause: str, match_value: Any) -> int:
        if r_multiple is not None:
            cursor = conn.execute(
                f"""
                UPDATE trades
                   SET status = ?,
                       profit_loss = ?,
                       r_multiple = ?,
                       close_time = ?
                 WHERE {where_clause}{filter_clause}
                """,
                (status, pnl, float(r_multiple), close_time, match_value, *filter_params),
            )
        else:
            cursor = conn.execute(
                f"""
                UPDATE trades
                   SET status = ?,
                       profit_loss = ?,
                       close_time = ?
                 WHERE {where_clause}{filter_clause}
                """,
                (status, pnl, close_time, match_value, *filter_params),
            )
        return int(cursor.rowcount or 0)

    with get_conn() as conn:
        updated = 0
        if position_ticket is not None:
            updated = _apply_update(conn, "position_ticket = ?", position_ticket)
        if updated == 0 and trade_id:
            updated = _apply_update(conn, "trade_id = ?", trade_id)
        if updated == 0 and ticket is not None:
            updated = _apply_update(conn, "trade_ticket = ?", ticket)
    return updated > 0


def insert_account_metrics(
    account_status: AccountStatus,
    *,
    evidence_context: EvidenceContext | None = None,
) -> None:
    evidence = evidence_context or _default_evidence_context()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO account_metrics (
                evidence_stream, policy_mode, execution_mode, account_scope,
                timestamp, balance, equity, open_risk_percent,
                open_usd_exposure_count, daily_loss_percent, weekly_loss_percent,
                drawdown_percent, peak_equity, daily_anchor_date, daily_anchor_equity,
                weekly_anchor_key, weekly_anchor_equity, consecutive_losses, is_trading_halted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_stream,
                evidence.policy_mode,
                evidence.execution_mode,
                evidence.account_scope,
                account_status.updated_at.isoformat(),
                account_status.balance,
                account_status.equity,
                account_status.open_risk_percent,
                account_status.open_usd_exposure_count,
                account_status.daily_loss_percent,
                account_status.weekly_loss_percent,
                account_status.drawdown_percent,
                account_status.peak_equity,
                account_status.daily_anchor_date,
                account_status.daily_anchor_equity,
                account_status.weekly_anchor_key,
                account_status.weekly_anchor_equity,
                account_status.consecutive_losses,
                int(account_status.is_trading_halted),
            ),
        )


def get_latest_account_metric(
    *,
    evidence_stream: str | None = None,
    account_scope: str | None = None,
) -> dict[str, Any] | None:
    clauses: list[str] = []
    params: list[Any] = []
    if evidence_stream:
        clauses.append("evidence_stream = ?")
        params.append(evidence_stream)
    if account_scope:
        clauses.append("account_scope = ?")
        params.append(account_scope)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT balance, equity, open_risk_percent, open_usd_exposure_count,
                   daily_loss_percent, weekly_loss_percent, drawdown_percent,
                   peak_equity, daily_anchor_date, daily_anchor_equity,
                   weekly_anchor_key, weekly_anchor_equity,
                   consecutive_losses, is_trading_halted, timestamp
              FROM account_metrics
              {where}
          ORDER BY timestamp DESC, id DESC
             LIMIT 1
            """,
            tuple(params),
        ).fetchone()
    return dict(row) if row is not None else None


def get_open_trade_ledger(
    *,
    evidence_stream: str | None = None,
    account_scope: str | None = None,
) -> dict[str, Any]:
    clauses = ["status IN ('EXECUTED_OPEN', 'EXECUTION_UNCERTAIN')"]
    params: list[Any] = []
    if evidence_stream:
        clauses.append("evidence_stream = ?")
        params.append(evidence_stream)
    if account_scope:
        clauses.append("account_scope = ?")
        params.append(account_scope)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT symbol, COALESCE(risk_percent, 0.0) AS risk_percent
              FROM trades
             WHERE {' AND '.join(clauses)}
            """,
            tuple(params),
        ).fetchall()
    return {
        "open_trade_count": len(rows),
        "open_risk_percent": round(
            sum(float(row["risk_percent"] or 0.0) for row in rows),
            8,
        ),
        "open_symbols": sorted({str(row["symbol"]) for row in rows if row["symbol"]}),
    }


def insert_risk_event(
    rule_name: str,
    severity: str,
    reason: str,
    trade_id: str | None = None,
    *,
    evidence_context: EvidenceContext | None = None,
) -> None:
    evidence = evidence_context or _default_evidence_context()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO risk_events (
                timestamp, evidence_stream, policy_mode, execution_mode, account_scope,
                rule_name, severity, reason, trade_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                evidence.evidence_stream,
                evidence.policy_mode,
                evidence.execution_mode,
                evidence.account_scope,
                rule_name,
                severity,
                reason,
                trade_id,
            ),
        )


def insert_decision_funnel_event(
    *,
    decision_time: datetime,
    stage: str,
    outcome: str,
    reason_code: str,
    symbol: str | None = None,
    details: str = "",
    trade_id: str | None = None,
    evidence_context: EvidenceContext | None = None,
) -> None:
    evidence = evidence_context or _default_evidence_context()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO decision_funnel_events (
                timestamp, evidence_stream, policy_mode, execution_mode, account_scope,
                decision_time, symbol, stage, outcome, reason_code, details, trade_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                evidence.evidence_stream,
                evidence.policy_mode,
                evidence.execution_mode,
                evidence.account_scope,
                decision_time.isoformat(),
                symbol,
                stage,
                outcome,
                reason_code,
                details,
                trade_id,
            ),
        )


def mark_trade_expired(trade_id: str, reason_code: str = "ROUTER_PENDING_EXPIRED") -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE trades
               SET status = ?,
                   reason_code = ?,
                   close_time = ?
             WHERE trade_id = ?
               AND status = 'PENDING'
            """,
            ("EXPIRED", reason_code, datetime.now(timezone.utc).isoformat(), trade_id),
        )


def get_recent_r_multiples(limit: int = 100) -> list[float]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r_multiple
              FROM trades
             WHERE status LIKE 'CLOSED%'
          ORDER BY close_time DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [float(row["r_multiple"]) for row in rows if row["r_multiple"] is not None]
