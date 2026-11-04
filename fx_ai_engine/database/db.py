from __future__ import annotations

import logging
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

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
def get_conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with the runtime safety settings applied."""
    resolved_path = DB_PATH if db_path is None else Path(db_path)
    conn = sqlite3.connect(resolved_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_schema() -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn() as conn:
        conn.executescript(schema)
        # WAL lets the Streamlit/read-only tooling inspect evidence without
        # needlessly blocking the engine's short write transactions.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")


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


def trade_exists(trade_id: str) -> bool:
    """Check if a trade_id already exists in the database (indexed lookup)."""
    if not trade_id:
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM trades WHERE trade_id = ? LIMIT 1", (trade_id,)).fetchone()
        return row is not None


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
    with get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS engine_runtime_state (
            evidence_stream TEXT NOT NULL, account_scope TEXT NOT NULL,
            key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (evidence_stream, account_scope, key)
        )""")


def get_engine_runtime_state(key: str, *, evidence_context: EvidenceContext) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM engine_runtime_state WHERE evidence_stream=? AND account_scope=? AND key=?",
            (evidence_context.evidence_stream, evidence_context.account_scope, key),
        ).fetchone()
    return None if row is None else str(row["value"])


def set_engine_runtime_state(key: str, value: str, *, evidence_context: EvidenceContext) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO engine_runtime_state (evidence_stream, account_scope, key, value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(evidence_stream, account_scope, key)
            DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (
                evidence_context.evidence_stream,
                evidence_context.account_scope,
                key,
                str(value),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def migrate_add_feedback_conflicts() -> None:
    with get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS execution_feedback_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_stream TEXT NOT NULL, account_scope TEXT NOT NULL,
            dedupe_key TEXT NOT NULL, trade_id TEXT NOT NULL,
            reason TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
            resolved_at TEXT, UNIQUE(evidence_stream, account_scope, dedupe_key)
        )""")


def migrate_add_trade_lifecycle_events() -> None:
    with get_conn() as conn:
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


def record_trade_lifecycle_event(
    trade_id: str,
    event_type: str,
    status: str,
    reason_code: str,
    payload: dict[str, Any] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    values = (
        trade_id,
        event_type,
        status,
        reason_code,
        json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str),
        datetime.now(timezone.utc).isoformat(),
    )
    sql = """
        INSERT INTO trade_lifecycle_events (
            trade_id, event_type, status, reason_code, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
    """
    if conn is not None:
        conn.execute(sql, values)
        return
    with get_conn() as owned_conn:
        owned_conn.execute(sql, values)


def migrate_add_trade_reservations() -> None:
    """Create the transactional pending-risk reservation ledger."""
    with get_conn() as conn:
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


_ACTIVE_RESERVATION_STATES = ("RESERVED", "ROUTED", "EXECUTION_UNCERTAIN")

# Terminal outcomes cannot be overwritten by a delayed route or feedback.
_RESERVATION_PREDECESSORS = {
    "ROUTED": ("RESERVED",),
    "EXECUTION_UNCERTAIN": ("RESERVED", "ROUTED", "EXECUTION_UNCERTAIN"),
    "EXECUTED": ("RESERVED", "ROUTED", "EXECUTION_UNCERTAIN", "EXECUTED"),
    "REJECTED": ("RESERVED", "ROUTED", "REJECTED"),
    "RELEASED": ("RESERVED", "RELEASED"),
    "EXPIRED": ("EXPIRED",),
    "CLOSED": ("EXECUTED", "CLOSED"),
}


def try_create_trade_reservation(
    *,
    reservation_id: str,
    trade_id: str,
    symbol: str,
    risk_percent: float,
    policy_hash: str,
    expires_at: str,
    max_positions: int,
    max_combined_exposure: float,
    broker_open_positions: int,
    broker_open_risk: float,
) -> dict[str, Any]:
    """Atomically reserve pending portfolio capacity.

    Broker-open exposure is supplied by the reconciled account snapshot; active
    reservations cover the gap between approval and broker acknowledgement.
    """
    now = datetime.now(timezone.utc).isoformat()
    if not reservation_id or not trade_id or risk_percent <= 0:
        return {"approved": False, "reason_code": "RESERVATION_INPUT_INVALID"}
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Signal expiry prevents new execution; it does not prove that an
        # earlier send was not filled. Retain capacity until a terminal result
        # or broker reconciliation explicitly resolves the reservation. This
        # also covers a crash between publishing the file and marking it routed.
        duplicate = conn.execute(
            "SELECT state FROM trade_reservations WHERE trade_id=? OR reservation_id=? LIMIT 1",
            (trade_id, reservation_id),
        ).fetchone()
        if duplicate is not None:
            return {"approved": False, "reason_code": "RESERVATION_DUPLICATE"}
        placeholders = ",".join("?" for _ in _ACTIVE_RESERVATION_STATES)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS reservation_count,
                   COALESCE(SUM(risk_percent), 0.0) AS reserved_risk
              FROM trade_reservations
             WHERE state IN ({placeholders})
            """,
            _ACTIVE_RESERVATION_STATES,
        ).fetchone()
        reserved_count = int(row["reservation_count"] or 0)
        reserved_risk = float(row["reserved_risk"] or 0.0)
        if int(broker_open_positions) + reserved_count >= int(max_positions):
            return {"approved": False, "reason_code": "RESERVATION_MAX_POSITIONS"}
        projected_risk = float(broker_open_risk) + reserved_risk + float(risk_percent)
        if projected_risk > float(max_combined_exposure) + 1e-12:
            return {"approved": False, "reason_code": "RESERVATION_MAX_EXPOSURE"}
        conn.execute(
            """
            INSERT INTO trade_reservations (
                reservation_id, trade_id, symbol, risk_percent, policy_hash,
                state, reason_code, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, 'RESERVED', 'RESERVATION_APPROVED', ?, ?, ?)
            """,
            (
                reservation_id,
                trade_id,
                symbol,
                float(risk_percent),
                policy_hash,
                now,
                now,
                expires_at,
            ),
        )
    return {
        "approved": True,
        "reason_code": "RESERVATION_APPROVED",
        "reservation_id": reservation_id,
        "projected_risk": projected_risk,
    }


def transition_trade_reservation(
    reservation_id: str,
    state: str,
    reason_code: str,
) -> bool:
    allowed = {
        "ROUTED",
        "EXECUTION_UNCERTAIN",
        "EXECUTED",
        "REJECTED",
        "RELEASED",
        "EXPIRED",
        "CLOSED",
    }
    normalized = str(state).upper()
    if normalized not in allowed:
        raise ValueError(f"Unsupported reservation state={state!r}")
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE trade_reservations
               SET state=?, reason_code=?, updated_at=?
             WHERE reservation_id=? AND state IN ({})
            """.format(",".join("?" for _ in _RESERVATION_PREDECESSORS[normalized])),
            (normalized, reason_code, now, reservation_id, *_RESERVATION_PREDECESSORS[normalized]),
        )
        return int(cursor.rowcount or 0) > 0


def transition_trade_reservation_by_trade_id(
    trade_id: str,
    state: str,
    reason_code: str,
) -> bool:
    allowed = {"EXECUTION_UNCERTAIN", "EXECUTED", "REJECTED", "RELEASED", "EXPIRED", "CLOSED"}
    normalized = str(state).upper()
    if normalized not in allowed:
        raise ValueError(f"Unsupported reservation state={state!r}")
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE trade_reservations SET state=?, reason_code=?, updated_at=? WHERE trade_id=? AND state IN ({})".format(",".join("?" for _ in _RESERVATION_PREDECESSORS[normalized])),
            (normalized, reason_code, now, trade_id, *_RESERVATION_PREDECESSORS[normalized]),
        )
        return int(cursor.rowcount or 0) > 0


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


def migrate_add_feedback_receipts() -> None:
    with get_conn() as conn:
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
    with get_conn() as conn:
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
    with get_conn() as conn:
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
    with get_conn() as conn:
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
    with get_conn() as conn:
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
            INSERT INTO trades (
                trade_id, evidence_stream, policy_mode, execution_mode, account_scope,
                symbol, direction, order_type, lot_size, 
                stop_loss, take_profit,
                risk_percent, market_regime, status, reason_code, open_time,
                regime_confidence, rsi_at_entry, atr_ratio,
                is_london_session, is_newyork_session, rate_differential, risk_reward, rsi_slope,
                spread_entry, spread_signal_pips
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_id) DO UPDATE SET
                evidence_stream=excluded.evidence_stream,
                policy_mode=excluded.policy_mode,
                execution_mode=excluded.execution_mode,
                account_scope=excluded.account_scope,
                symbol=excluded.symbol,
                direction=excluded.direction,
                order_type=excluded.order_type,
                stop_loss=excluded.stop_loss,
                take_profit=excluded.take_profit,
                risk_percent=excluded.risk_percent,
                market_regime=excluded.market_regime,
                status=excluded.status,
                reason_code=excluded.reason_code,
                regime_confidence=excluded.regime_confidence,
                rsi_at_entry=excluded.rsi_at_entry,
                atr_ratio=excluded.atr_ratio,
                is_london_session=excluded.is_london_session,
                is_newyork_session=excluded.is_newyork_session,
                rate_differential=excluded.rate_differential,
                risk_reward=excluded.risk_reward,
                rsi_slope=excluded.rsi_slope,
                spread_entry=excluded.spread_entry,
                spread_signal_pips=excluded.spread_signal_pips
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
        record_trade_lifecycle_event(
            signal.trade_id,
            "PROPOSAL",
            status,
            reason_code,
            {"symbol": signal.symbol, "risk_percent": risk_percent, "market_regime": market_regime},
            conn=conn,
        )


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def apply_execution_feedback(payload: dict[str, Any], *, dedupe_key: str, source_file: str,
                             evidence_context: EvidenceContext) -> str:
    """Apply execution evidence atomically; return applied, duplicate, unmatched or conflict.

    The trade determines account scope because reservations predate evidence scoping.
    A broker send failure is not proof that an order did not reach the broker.
    """
    from core.schemas import validate_execution_feedback

    validate_execution_feedback(payload)
    trade_id, status = payload["trade_id"], payload["status"]
    now = datetime.now(timezone.utc).isoformat()
    evidence = evidence_context
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        trade = conn.execute("SELECT * FROM trades WHERE trade_id=? AND evidence_stream=? AND account_scope=?",
                             (trade_id, evidence.evidence_stream, evidence.account_scope)).fetchone()
        reservation = conn.execute("SELECT * FROM trade_reservations WHERE trade_id=?", (trade_id,)).fetchone()

        def conflict(reason: str, result: str = "conflict") -> str:
            conn.execute("""INSERT OR IGNORE INTO execution_feedback_conflicts
                (evidence_stream, account_scope, dedupe_key, trade_id, reason, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (evidence.evidence_stream, evidence.account_scope, dedupe_key,
                trade_id, reason, json.dumps(payload, sort_keys=True, default=str), now))
            return result

        if conn.execute("SELECT 1 FROM feedback_receipts WHERE evidence_stream=? AND account_scope=? AND feedback_kind='execution' AND dedupe_key=?",
                        (evidence.evidence_stream, evidence.account_scope, dedupe_key)).fetchone():
            # Existing receipt keys do not include price or quantity. Compare
            # retained evidence before treating the same key as a retry.
            rows = conn.execute("SELECT payload_json FROM trade_lifecycle_events WHERE trade_id=? AND event_type='EXECUTION' ORDER BY id DESC", (trade_id,)).fetchall()
            for row in rows:
                previous = json.loads(row["payload_json"])
                if (previous.get("status"), previous.get("ticket"), previous.get("close_time")) == (status, payload["ticket"], payload["close_time"]):
                    if any(previous.get(key) != payload.get(key) for key in ("entry_price", "lot_size", "position_ticket")):
                        return conflict("RECEIPT_EVIDENCE_CONFLICT")
                    break
            return "duplicate"
        if trade is None:
            return conflict("TRADE_NOT_FOUND_IN_EVIDENCE_SCOPE", "unmatched")
        if reservation is None:
            return conflict("RESERVATION_NOT_FOUND", "unmatched")

        state = reservation["state"]
        target = "EXECUTED" if status == "EXECUTED" else ("EXECUTION_UNCERTAIN" if status in {"REJECTED_ORDER_SEND", "EXECUTION_UNCERTAIN"} else "REJECTED")
        if state not in _RESERVATION_PREDECESSORS[target]:
            return conflict(f"RESERVATION_TRANSITION_CONFLICT:{state}->{target}")
        if state == target and target in {"EXECUTED", "REJECTED"}:
            same = (target == "EXECUTED" and trade["status"] == "EXECUTED_OPEN"
                    and trade["trade_ticket"] == payload["ticket"]
                    and trade["position_ticket"] == (payload.get("position_ticket") or payload["ticket"])
                    and trade["entry_price"] == payload["entry_price"] and trade["lot_size"] == payload["lot_size"])
            same = same or (target == "REJECTED" and trade["status"] == "REJECTED" and trade["reason_code"] == status)
            if not same:
                return conflict("TERMINAL_EVIDENCE_CONFLICT")
            return "duplicate"
        if trade["status"] not in ("PENDING", "EXECUTION_UNCERTAIN"):
            return conflict(f"TRADE_STATE_CONFLICT:{trade['status']}")

        trade_status = {"EXECUTED": "EXECUTED_OPEN", "REJECTED": "REJECTED", "EXECUTION_UNCERTAIN": "EXECUTION_UNCERTAIN"}[target]
        conn.execute("""UPDATE trades SET status=?, reason_code=?, trade_ticket=?, position_ticket=?,
            entry_price=?, lot_size=?, slippage=?, spread_exec_price=?, execution_time=?
            WHERE trade_id=? AND evidence_stream=? AND account_scope=?""",
            (trade_status, status, payload["ticket"] if target == "EXECUTED" else None,
             (payload.get("position_ticket") or payload["ticket"]) if target == "EXECUTED" else None,
             payload["entry_price"] if target == "EXECUTED" else trade["entry_price"],
             payload["lot_size"] if target == "EXECUTED" else trade["lot_size"],
             payload["slippage"], payload["spread_at_entry"],
             (payload.get("execution_time") or payload["close_time"] or now) if target == "EXECUTED" else None,
             trade_id, evidence.evidence_stream, evidence.account_scope))
        record_trade_lifecycle_event(trade_id, "EXECUTION", trade_status, status, payload, conn=conn)
        conn.execute("UPDATE trade_reservations SET state=?, reason_code=?, updated_at=? WHERE trade_id=?",
                     (target, status, now, trade_id))
        conn.execute("""INSERT INTO feedback_receipts (timestamp, evidence_stream, policy_mode, execution_mode,
            account_scope, feedback_kind, dedupe_key, trade_id, ticket, status, close_time, source_file)
            VALUES (?, ?, ?, ?, ?, 'execution', ?, ?, ?, ?, ?, ?)""",
            (now, evidence.evidence_stream, evidence.policy_mode, evidence.execution_mode, evidence.account_scope,
             dedupe_key, trade_id, payload["ticket"] or None, status, payload["close_time"], source_file))
        conn.execute("""UPDATE execution_feedback_conflicts SET resolved_at=? WHERE evidence_stream=? AND account_scope=?
            AND dedupe_key=? AND reason IN ('TRADE_NOT_FOUND_IN_EVIDENCE_SCOPE', 'RESERVATION_NOT_FOUND')""",
            (now, evidence.evidence_stream, evidence.account_scope, dedupe_key))
    return "applied"


def list_unresolved_execution_feedback_conflicts() -> list[dict[str, Any]]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT id, evidence_stream, account_scope, dedupe_key, trade_id, reason,
            payload_json, created_at FROM execution_feedback_conflicts WHERE resolved_at IS NULL ORDER BY id""").fetchall()
    return [dict(row) for row in rows]


def resolve_execution_feedback_conflict(conflict_id: int, *, operator_note: str, now_utc: str | None = None) -> bool:
    """Mark one conflict resolved and record the operator's broker evidence as a risk event.

    The note must describe the evidence checked (orders, deals, positions, EA log);
    resolution never changes trades or reservations.
    """
    note = str(operator_note or "").strip()
    if len(note) < 20:
        raise ValueError("operator_note must describe the broker evidence checked (at least 20 characters)")
    now = now_utc or datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute("""SELECT trade_id, reason, evidence_stream, account_scope FROM execution_feedback_conflicts
            WHERE id=? AND resolved_at IS NULL""", (int(conflict_id),)).fetchone()
        if row is None:
            return False
        stream = str(row[2] or "")
        # Evidence streams are "runtime_<execution_mode>_<policy_mode>[__<experiment>]".
        execution_mode, _, policy_part = stream.removeprefix("runtime_").partition("_")
        policy_mode = policy_part.split("__")[0] or "unknown"
        conn.execute("UPDATE execution_feedback_conflicts SET resolved_at=? WHERE id=?", (now, int(conflict_id)))
        conn.execute("""INSERT INTO risk_events (timestamp, rule_name, severity, reason, trade_id, evidence_stream,
            policy_mode, execution_mode, account_scope) VALUES (?, 'FEEDBACK_CONFLICT_RESOLVED', 'INFO', ?, ?, ?, ?, ?, ?)""",
            (now, f"conflict_id={conflict_id} reason={row[1]} operator_note={note}", row[0], stream,
             policy_mode, execution_mode or "unknown", row[3]))
    return True


def has_unresolved_execution_feedback(evidence_context: EvidenceContext) -> bool:
    with get_conn() as conn:
        conflict = conn.execute("SELECT 1 FROM execution_feedback_conflicts WHERE evidence_stream=? AND account_scope=? AND resolved_at IS NULL LIMIT 1",
                                (evidence_context.evidence_stream, evidence_context.account_scope)).fetchone()
        uncertain = conn.execute("""SELECT 1 FROM trade_reservations r LEFT JOIN trades t ON t.trade_id=r.trade_id
            WHERE r.state IN ('RESERVED', 'ROUTED', 'EXECUTION_UNCERTAIN')
            AND (r.state='EXECUTION_UNCERTAIN' OR t.trade_id IS NULL OR julianday(r.expires_at) IS NULL
                 OR julianday(r.expires_at) <= julianday('now'))
            AND (t.trade_id IS NULL OR (t.evidence_stream=? AND t.account_scope=?)) LIMIT 1""",
            (evidence_context.evidence_stream, evidence_context.account_scope)).fetchone()
        return bool(conflict or uncertain)


def update_trade_execution_result(payload: dict[str, Any]) -> bool:
    raw_status = str(payload.get("status", "UNKNOWN")).upper()
    ticket = _positive_int(payload.get("ticket")) or 0
    position_ticket = _positive_int(payload.get("position_ticket"))
    entry_price = _positive_float(payload.get("entry_price"))
    lot_size = _positive_float(payload.get("lot_size"))
    executed_open = raw_status == "EXECUTED" and ticket > 0
    rejected = raw_status.startswith("REJECTED")
    status = "EXECUTED_OPEN" if executed_open else ("REJECTED" if rejected else raw_status)
    reason_code = raw_status

    with get_conn() as conn:
        if executed_open:
            cursor = conn.execute(
                """
                UPDATE trades
                   SET trade_ticket = ?,
                       position_ticket = ?,
                       status = ?,
                       lot_size = COALESCE(?, lot_size),
                       entry_price = COALESCE(?, entry_price),
                       slippage = ?,
                       spread_exec_price = ?,
                       execution_time = ?
                 WHERE trade_id = ?
                """,
                (
                    ticket,
                    position_ticket or ticket,
                    status,
                    lot_size,
                    entry_price,
                    payload.get("slippage", 0.0),
                    payload.get("spread_at_entry", 0.0),
                    payload.get("execution_time")
                    or payload.get("close_time")
                    or datetime.now(timezone.utc).isoformat(),
                    payload.get("trade_id"),
                ),
            )
            updated = int(cursor.rowcount or 0) > 0
            if updated:
                record_trade_lifecycle_event(
                    str(payload.get("trade_id", "")),
                    "EXECUTION",
                    status,
                    reason_code,
                    payload,
                    conn=conn,
                )
            return updated

        close_time = payload.get("close_time", datetime.now(timezone.utc).isoformat())
        cursor = conn.execute(
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
        updated = int(cursor.rowcount or 0) > 0
        if updated:
            record_trade_lifecycle_event(
                str(payload.get("trade_id", "")),
                "EXECUTION",
                status,
                reason_code,
                payload,
                conn=conn,
            )
        return updated


def mark_trade_execution_uncertain(
    trade_id: str,
    reason_code: str = "PRESERVE_10_EXECUTION_UNCERTAIN",
) -> None:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE trades
               SET status = ?,
                   reason_code = ?
             WHERE trade_id = ?
               AND status IN ('PENDING', 'EXECUTION_UNCERTAIN')
            """,
            ("EXECUTION_UNCERTAIN", reason_code, trade_id),
        )
        if int(cursor.rowcount or 0) > 0:
            conn.execute("""UPDATE trade_reservations SET state='EXECUTION_UNCERTAIN',
                reason_code=?, updated_at=? WHERE trade_id=? AND state IN ('RESERVED', 'ROUTED')""",
                (reason_code, datetime.now(timezone.utc).isoformat(), trade_id))
            record_trade_lifecycle_event(
                trade_id,
                "EXECUTION_UNCERTAIN",
                "EXECUTION_UNCERTAIN",
                reason_code,
                conn=conn,
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
        if updated > 0 and trade_id:
            record_trade_lifecycle_event(
                trade_id,
                "EXIT",
                status,
                raw_status,
                payload,
                conn=conn,
            )
    return updated > 0


def get_trade_by_identifiers(
    *,
    trade_id: str | None = None,
    position_ticket: int | None = None,
    trade_ticket: int | None = None,
    evidence_context: EvidenceContext | None = None,
) -> dict[str, Any] | None:
    """Fetch a single trade row using any available identifiers with optional evidence scoping."""

    clauses: list[str] = []
    params: list[Any] = []

    if position_ticket is not None:
        clauses.append("position_ticket = ?")
        params.append(_positive_int(position_ticket))
    if trade_ticket is not None:
        clauses.append("trade_ticket = ?")
        params.append(_positive_int(trade_ticket))
    if trade_id:
        clauses.append("trade_id = ?")
        params.append(str(trade_id))

    if evidence_context is not None:
        clauses.append("evidence_stream = ?")
        clauses.append("account_scope = ?")
        params.extend([evidence_context.evidence_stream, evidence_context.account_scope])

    where = f"WHERE {' OR '.join(clauses)}" if clauses else ""
    if not clauses:
        return None

    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT trade_id, symbol, direction, stop_loss, take_profit, lot_size,
                   entry_price, r_multiple, status
              FROM trades
              {where}
          ORDER BY id DESC
             LIMIT 1
            """,
            tuple(params),
        ).fetchone()

    return dict(row) if row is not None else None


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
    rows = list_open_trades(evidence_stream=evidence_stream, account_scope=account_scope)
    open_trade_ids = sorted(str(row["trade_id"]) for row in rows if row["trade_id"])
    open_trade_tickets = sorted(
        {
            int(row["trade_ticket"])
            for row in rows
            if _positive_int(row["trade_ticket"]) is not None
        }
    )
    open_position_tickets = sorted(
        {
            int(row["position_ticket"])
            for row in rows
            if _positive_int(row["position_ticket"]) is not None
        }
    )
    open_statuses = sorted({str(row["status"]) for row in rows if row["status"]})
    return {
        "open_trade_count": len(rows),
        "open_risk_percent": round(
            sum(float(row["risk_percent"] or 0.0) for row in rows),
            8,
        ),
        "open_symbols": sorted({str(row["symbol"]) for row in rows if row["symbol"]}),
        "open_trade_ids": open_trade_ids,
        "open_trade_tickets": open_trade_tickets,
        "open_position_tickets": open_position_tickets,
        "open_statuses": open_statuses,
    }


def list_open_trades(
    *,
    evidence_stream: str | None = None,
    account_scope: str | None = None,
) -> list[dict[str, Any]]:
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
            SELECT trade_id,
                   trade_ticket,
                   position_ticket,
                   symbol,
                   direction,
                   status,
                   COALESCE(risk_percent, 0.0) AS risk_percent,
                   COALESCE(stop_loss, 0.0) AS stop_loss,
                   COALESCE(take_profit, 0.0) AS take_profit,
                   COALESCE(entry_price, 0.0) AS entry_price,
                   COALESCE(lot_size, 0.0) AS lot_size,
                   open_time,
                   execution_time
              FROM trades
             WHERE {' AND '.join(clauses)}
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def _legacy_partition_where_clause() -> str:
    return (
        "COALESCE(NULLIF(TRIM(evidence_stream), ''), '') = ? "
        "OR COALESCE(NULLIF(TRIM(policy_mode), ''), '') = ? "
        "OR COALESCE(NULLIF(TRIM(execution_mode), ''), '') = 'legacy' "
        "OR COALESCE(NULLIF(TRIM(account_scope), ''), '') = ?"
    )


def count_legacy_partition_rows() -> dict[str, int]:
    table_counts: dict[str, int] = {}
    with get_conn() as conn:
        for table in ("trades", "account_metrics", "risk_events", "decision_funnel_events", "ml_shadow_events"):
            if not _table_exists(conn, table):
                table_counts[table] = 0
                continue
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS n
                  FROM {table}
                 WHERE {_legacy_partition_where_clause()}
                """,
                (
                    LEGACY_UNPARTITIONED_STREAM,
                    LEGACY_UNPARTITIONED_STREAM,
                    LEGACY_UNPARTITIONED_ACCOUNT_SCOPE,
                ),
            ).fetchone()
            table_counts[table] = int(row["n"] or 0)
    table_counts["total"] = sum(table_counts.values())
    return table_counts


def _create_archive_table_like(
    conn: sqlite3.Connection,
    source_table: str,
    archive_table: str,
) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {archive_table} AS
        SELECT source.*,
               CAST('' AS TEXT) AS archived_at,
               CAST('' AS TEXT) AS archive_reason
          FROM {source_table} AS source
         WHERE 0
        """
    )


def archive_legacy_partition_rows(*, archive_reason: str = "manual_archive") -> dict[str, int]:
    archived_at = datetime.now(timezone.utc).isoformat()
    archived_counts: dict[str, int] = {}
    with get_conn() as conn:
        for table in ("trades", "account_metrics", "risk_events", "decision_funnel_events", "ml_shadow_events"):
            if not _table_exists(conn, table):
                archived_counts[table] = 0
                continue
            archive_table = f"{table}_archive"
            _create_archive_table_like(conn, table, archive_table)
            rows = conn.execute(
                f"""
                SELECT *
                  FROM {table}
                 WHERE {_legacy_partition_where_clause()}
                """,
                (
                    LEGACY_UNPARTITIONED_STREAM,
                    LEGACY_UNPARTITIONED_STREAM,
                    LEGACY_UNPARTITIONED_ACCOUNT_SCOPE,
                ),
            ).fetchall()
            archived_counts[table] = len(rows)
            if not rows:
                continue

            source_columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            archive_columns = source_columns + ["archived_at", "archive_reason"]
            placeholders = ", ".join("?" for _ in archive_columns)
            conn.executemany(
                f"""
                INSERT INTO {archive_table} ({", ".join(archive_columns)})
                VALUES ({placeholders})
                """,
                [
                    tuple(row[column] for column in source_columns) + (archived_at, archive_reason)
                    for row in rows
                ],
            )
            conn.execute(
                f"""
                DELETE FROM {table}
                 WHERE {_legacy_partition_where_clause()}
                """,
                (
                    LEGACY_UNPARTITIONED_STREAM,
                    LEGACY_UNPARTITIONED_STREAM,
                    LEGACY_UNPARTITIONED_ACCOUNT_SCOPE,
                ),
            )
    archived_counts["total"] = sum(archived_counts.values())
    return archived_counts


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


def insert_ml_shadow_event(
    *,
    decision_time: datetime,
    symbol: str,
    stage: str,
    primary_gate_outcome: str,
    shadow_outcome: str,
    reason_code: str,
    details: str = "",
    trade_id: str | None = None,
    probability: float | None = None,
    threshold: float | None = None,
    model_loaded: bool = False,
    checkpoint_path: str | None = None,
    feature_schema_version: str | None = None,
    evidence_context: EvidenceContext | None = None,
) -> None:
    evidence = evidence_context or _default_evidence_context()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ml_shadow_events (
                timestamp,
                evidence_stream,
                policy_mode,
                execution_mode,
                account_scope,
                decision_time,
                symbol,
                trade_id,
                stage,
                primary_gate_outcome,
                shadow_outcome,
                reason_code,
                details,
                probability,
                threshold,
                model_loaded,
                checkpoint_path,
                feature_schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                evidence.evidence_stream,
                evidence.policy_mode,
                evidence.execution_mode,
                evidence.account_scope,
                decision_time.isoformat(),
                symbol,
                trade_id,
                stage,
                primary_gate_outcome,
                shadow_outcome,
                reason_code,
                details,
                probability,
                threshold,
                int(bool(model_loaded)),
                checkpoint_path,
                feature_schema_version,
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


def feedback_receipt_exists(
    feedback_kind: str,
    dedupe_key: str,
    *,
    evidence_context: EvidenceContext | None = None,
) -> bool:
    evidence = evidence_context or _default_evidence_context()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1
              FROM feedback_receipts
             WHERE evidence_stream = ?
               AND account_scope = ?
               AND feedback_kind = ?
               AND dedupe_key = ?
             LIMIT 1
            """,
            (
                evidence.evidence_stream,
                evidence.account_scope,
                feedback_kind,
                dedupe_key,
            ),
        ).fetchone()
    return row is not None


def record_feedback_receipt(
    feedback_kind: str,
    dedupe_key: str,
    *,
    trade_id: str | None = None,
    ticket: int | None = None,
    status: str | None = None,
    close_time: str | None = None,
    source_file: str | None = None,
    evidence_context: EvidenceContext | None = None,
) -> bool:
    """Persist a feedback receipt. Returns True when inserted, False on duplicate."""
    evidence = evidence_context or _default_evidence_context()
    ticket_value = _positive_int(ticket)
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO feedback_receipts (
                timestamp,
                evidence_stream,
                policy_mode,
                execution_mode,
                account_scope,
                feedback_kind,
                dedupe_key,
                trade_id,
                ticket,
                status,
                close_time,
                source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                evidence.evidence_stream,
                evidence.policy_mode,
                evidence.execution_mode,
                evidence.account_scope,
                feedback_kind,
                dedupe_key,
                trade_id,
                ticket_value,
                status,
                close_time,
                source_file,
            ),
        )
        inserted = int(cursor.rowcount or 0) > 0
        if not inserted:
            conn.execute(
                """
                UPDATE feedback_receipts
                   SET timestamp = ?
                 WHERE evidence_stream = ?
                   AND account_scope = ?
                   AND feedback_kind = ?
                   AND dedupe_key = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    evidence.evidence_stream,
                    evidence.account_scope,
                    feedback_kind,
                    dedupe_key,
                ),
            )
        return inserted
