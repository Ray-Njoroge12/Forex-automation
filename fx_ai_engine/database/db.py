"""Public persistence API (facade).

The implementation lives in ``database.connection``, ``database.migrations`` and
``database.repositories.{trades,risk,config}`` (Phase 4 split, behaviour
unchanged). Import from here as before. To point the code at another database
file in tests or tools, patch ``database.connection.DB_PATH`` or
``database.connection.get_conn``; the copies re-exported here are not read at
call time.
"""
from __future__ import annotations

from database.connection import (  # noqa: F401
    DB_PATH,
    SCHEMA_PATH,
    get_conn,
    _column_exists,
    _table_exists,
    _ensure_column,
)
from database.migrations import (  # noqa: F401
    initialize_schema,
    run_all_migrations,
    migrate_add_engine_runtime_state,
    migrate_add_feedback_conflicts,
    migrate_add_trade_lifecycle_events,
    migrate_add_trade_reservations,
    migrate_phase8_columns,
    migrate_add_restart_state_columns,
    migrate_add_risk_events,
    migrate_add_decision_funnel_events,
    migrate_add_feedback_receipts,
    _default_evidence_context,
    migrate_add_evidence_partition_columns,
    migrate_mark_stale_trades,
    migrate_add_ml_feature_columns,
    migrate_add_ml_shadow_events,
)
from database.repositories.trades import (  # noqa: F401
    trade_exists,
    record_trade_lifecycle_event,
    insert_trade_proposal,
    _positive_int,
    _positive_float,
    apply_execution_feedback,
    list_unresolved_execution_feedback_conflicts,
    resolve_execution_feedback_conflict,
    has_unresolved_execution_feedback,
    update_trade_execution_result,
    mark_trade_execution_uncertain,
    update_trade_exit_result,
    get_trade_by_identifiers,
    get_open_trade_ledger,
    list_open_trades,
    _legacy_partition_where_clause,
    count_legacy_partition_rows,
    _create_archive_table_like,
    archive_legacy_partition_rows,
    insert_decision_funnel_event,
    insert_ml_shadow_event,
    mark_trade_expired,
    get_recent_r_multiples,
    feedback_receipt_exists,
    record_feedback_receipt,
)
from database.repositories.risk import (  # noqa: F401
    _ACTIVE_RESERVATION_STATES,
    _RESERVATION_PREDECESSORS,
    try_create_trade_reservation,
    transition_trade_reservation,
    transition_trade_reservation_by_trade_id,
    insert_account_metrics,
    get_latest_account_metric,
    insert_risk_event,
)
from database.repositories.config import (  # noqa: F401
    get_engine_runtime_state,
    set_engine_runtime_state,
)
