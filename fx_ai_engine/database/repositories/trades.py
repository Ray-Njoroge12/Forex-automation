"""Trade persistence: proposals, execution and exit feedback, lifecycle events and receipts."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from core.evidence import (
    EvidenceContext,
    LEGACY_UNPARTITIONED_ACCOUNT_SCOPE,
    LEGACY_UNPARTITIONED_STREAM,
)
from core.types import TechnicalSignal
from database import connection as _connection
from database.connection import _table_exists
from database.migrations import _default_evidence_context
from database.repositories.risk import _RESERVATION_PREDECESSORS


def trade_exists(trade_id: str) -> bool:
    """Check if a trade_id already exists in the database (indexed lookup)."""
    if not trade_id:
        return False
    with _connection.get_conn() as conn:
        row = conn.execute("SELECT 1 FROM trades WHERE trade_id = ? LIMIT 1", (trade_id,)).fetchone()
        return row is not None


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
    with _connection.get_conn() as owned_conn:
        owned_conn.execute(sql, values)


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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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

    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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

    with _connection.get_conn() as conn:
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

    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
