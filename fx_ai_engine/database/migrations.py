"""Schema initialisation and the idempotent migration runner."""
from __future__ import annotations

import logging

from core.evidence import (
    EvidenceContext,
    LEGACY_UNPARTITIONED_ACCOUNT_SCOPE,
    LEGACY_UNPARTITIONED_STREAM,
)
from database import connection as _connection
from database.connection import _ensure_column, _table_exists

# Same logger name as before the split so log routing is unchanged.
logger = logging.getLogger("database.db")


def initialize_schema() -> None:
    schema = _connection.SCHEMA_PATH.read_text(encoding="utf-8")
    with _connection.get_conn() as conn:
        conn.executescript(schema)
        # WAL lets the Streamlit/read-only tooling inspect evidence without
        # needlessly blocking the engine's short write transactions.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")


def run_all_migrations() -> None:
    """Execute all schema migrations in correct order idempotently."""
    initialize_schema()
    migrate_phase8_columns()
    migrate_add_restart_state_columns()
    migrate_add_risk_events()
    migrate_add_decision_funnel_events()
    migrate_add_feedback_receipts()
    migrate_add_evidence_partition_columns()
    migrate_mark_stale_trades()
    migrate_add_ml_feature_columns()
    migrate_add_ml_shadow_events()
    migrate_add_trade_reservations()
    migrate_add_trade_lifecycle_events()
    migrate_add_feedback_conflicts()
    migrate_add_engine_runtime_state()


def migrate_add_engine_runtime_state() -> None:
    """Small key/value store for engine state that must survive a restart."""
    with _connection.get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS engine_runtime_state (
            evidence_stream TEXT NOT NULL, account_scope TEXT NOT NULL,
            key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (evidence_stream, account_scope, key)
        )""")


def migrate_add_feedback_conflicts() -> None:
    with _connection.get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS execution_feedback_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_stream TEXT NOT NULL, account_scope TEXT NOT NULL,
            dedupe_key TEXT NOT NULL, trade_id TEXT NOT NULL,
            reason TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
            resolved_at TEXT, UNIQUE(evidence_stream, account_scope, dedupe_key)
        )""")


def migrate_add_trade_lifecycle_events() -> None:
    with _connection.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                payload_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_lifecycle_trade_time ON trade_lifecycle_events(trade_id, created_at)"
        )


def migrate_add_trade_reservations() -> None:
    """Create the transactional pending-risk reservation ledger."""
    with _connection.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_reservations (
                reservation_id TEXT PRIMARY KEY,
                trade_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                risk_percent REAL NOT NULL,
                policy_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trade_reservations_state_expiry
                ON trade_reservations(state, expires_at)
            """
        )


def migrate_phase8_columns() -> None:
    """
    Adds Phase 8 columns for users that initialized earlier baseline schema.
    SQLite CREATE TABLE IF NOT EXISTS does not backfill new columns.
    """
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
        _ensure_column(conn, "account_metrics", "peak_equity", "REAL DEFAULT 0.0")
        _ensure_column(conn, "account_metrics", "daily_anchor_date", "TEXT DEFAULT ''")
        _ensure_column(conn, "account_metrics", "daily_anchor_equity", "REAL DEFAULT 0.0")
        _ensure_column(conn, "account_metrics", "weekly_anchor_key", "TEXT DEFAULT ''")
        _ensure_column(conn, "account_metrics", "weekly_anchor_equity", "REAL DEFAULT 0.0")


def migrate_add_risk_events() -> None:
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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


def migrate_add_feedback_receipts() -> None:
    with _connection.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                evidence_stream TEXT DEFAULT 'legacy_unpartitioned',
                policy_mode TEXT DEFAULT 'legacy_unpartitioned',
                execution_mode TEXT DEFAULT 'legacy',
                account_scope TEXT DEFAULT 'legacy_unpartitioned',
                feedback_kind TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                trade_id TEXT,
                ticket INTEGER,
                status TEXT,
                close_time TEXT,
                source_file TEXT,
                UNIQUE(evidence_stream, account_scope, feedback_kind, dedupe_key)
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_receipts_scope_kind_key
                ON feedback_receipts(evidence_stream, account_scope, feedback_kind, dedupe_key)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_feedback_receipts_timestamp
                ON feedback_receipts(timestamp)
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
    with _connection.get_conn() as conn:
        for table in ("trades", "account_metrics", "risk_events", "decision_funnel_events", "ml_shadow_events"):
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


def migrate_mark_stale_trades() -> None:
    """One-time cleanup: mark trades stuck in EXECUTED/PENDING with no feedback as FEEDBACK_LOST.

    These are early trades (typically from initial sessions) where bridge path
    misconfiguration prevented execution feedback from being consumed.  Idempotent.
    """
    with _connection.get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE trades
               SET status = 'FEEDBACK_LOST',
                   reason_code = 'STALE_NO_FEEDBACK'
             WHERE status IN ('EXECUTED', 'PENDING')
               AND COALESCE(lot_size, 0) = 0
               AND COALESCE(profit_loss, 0) = 0
               AND COALESCE(close_time, '') = ''
            """
        )
        if cur.rowcount:
            logger.info("Marked %d stale trades as FEEDBACK_LOST", cur.rowcount)


def migrate_add_ml_feature_columns() -> None:
    """Adds ML feature columns required for SignalRanker training. Idempotent."""
    with _connection.get_conn() as conn:
        _ensure_column(conn, "trades", "regime_confidence", "REAL")
        _ensure_column(conn, "trades", "rsi_at_entry", "REAL")
        _ensure_column(conn, "trades", "atr_ratio", "REAL")
        _ensure_column(conn, "trades", "is_london_session", "INTEGER")
        _ensure_column(conn, "trades", "is_newyork_session", "INTEGER")
        _ensure_column(conn, "trades", "rate_differential", "REAL")
        _ensure_column(conn, "trades", "risk_reward", "REAL")
        _ensure_column(conn, "trades", "rsi_slope", "REAL")


def migrate_add_ml_shadow_events() -> None:
    """Creates observe-only ML shadow telemetry table. Idempotent."""
    with _connection.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ml_shadow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                evidence_stream TEXT DEFAULT 'legacy_unpartitioned',
                policy_mode TEXT DEFAULT 'legacy_unpartitioned',
                execution_mode TEXT DEFAULT 'legacy',
                account_scope TEXT DEFAULT 'legacy_unpartitioned',
                decision_time DATETIME NOT NULL,
                symbol TEXT NOT NULL,
                trade_id TEXT,
                stage TEXT NOT NULL,
                primary_gate_outcome TEXT NOT NULL,
                shadow_outcome TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                details TEXT DEFAULT '',
                probability REAL,
                threshold REAL,
                model_loaded INTEGER DEFAULT 0,
                checkpoint_path TEXT,
                feature_schema_version TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ml_shadow_scope_time
                ON ml_shadow_events(evidence_stream, account_scope, decision_time)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ml_shadow_trade
                ON ml_shadow_events(trade_id, decision_time)
            """
        )
