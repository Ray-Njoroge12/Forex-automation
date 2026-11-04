"""Risk persistence: trade reservations, account metrics and risk events."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.account_status import AccountStatus
from core.evidence import EvidenceContext
from database import connection as _connection
from database.migrations import _default_evidence_context


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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE trade_reservations SET state=?, reason_code=?, updated_at=? WHERE trade_id=? AND state IN ({})".format(",".join("?" for _ in _RESERVATION_PREDECESSORS[normalized])),
            (normalized, reason_code, now, trade_id, *_RESERVATION_PREDECESSORS[normalized]),
        )
        return int(cursor.rowcount or 0) > 0


def insert_account_metrics(
    account_status: AccountStatus,
    *,
    evidence_context: EvidenceContext | None = None,
) -> None:
    evidence = evidence_context or _default_evidence_context()
    with _connection.get_conn() as conn:
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
    with _connection.get_conn() as conn:
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


def insert_risk_event(
    rule_name: str,
    severity: str,
    reason: str,
    trade_id: str | None = None,
    *,
    evidence_context: EvidenceContext | None = None,
) -> None:
    evidence = evidence_context or _default_evidence_context()
    with _connection.get_conn() as conn:
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
