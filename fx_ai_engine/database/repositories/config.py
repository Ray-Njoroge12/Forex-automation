"""Engine runtime settings that must survive a restart (key/value per evidence scope)."""
from __future__ import annotations

from datetime import datetime, timezone

from core.evidence import EvidenceContext
from database import connection as _connection


def get_engine_runtime_state(key: str, *, evidence_context: EvidenceContext) -> str | None:
    with _connection.get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM engine_runtime_state WHERE evidence_stream=? AND account_scope=? AND key=?",
            (evidence_context.evidence_stream, evidence_context.account_scope, key),
        ).fetchone()
    return None if row is None else str(row["value"])


def set_engine_runtime_state(key: str, value: str, *, evidence_context: EvidenceContext) -> None:
    with _connection.get_conn() as conn:
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
