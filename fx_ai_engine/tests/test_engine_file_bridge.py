"""Engine start-up and one loop on the EA file bridge (no MT5 package, no orders)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from core.file_bridge_connection import FileBridgeConnection
from core.metrics import init_metrics
from core.observability import init_tracing
from database import db
from tests.test_file_bridge_connection import build_bridge


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "state.db"

    @contextmanager
    def connection(db_path=None):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(db, "get_conn", connection)
    db.initialize_schema()
    return connection


def test_engine_runs_one_loop_on_file_bridge(tmp_path, monkeypatch, isolated_db):
    import main

    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    monkeypatch.delenv("USE_MT5_MOCK", raising=False)
    monkeypatch.setenv("BRIDGE_BASE_PATH", str(bridge_dir))
    monkeypatch.setattr(main, "update_health_status", lambda **_: None)

    conn = FileBridgeConnection(bridge_dir)
    assert conn.connect(retry_count=1)
    assert main._demo_account_verified(conn, use_mock=False)

    engine = main.Engine(conn, init_tracing("test"), init_metrics(), use_mock=False, run_mode="demo")
    assert engine.evidence_context.account_scope == "mt5:MetaQuotes-Demo:5058284077"
    engine.run(mode="demo", iterations=1)

    status = engine.account_status
    assert status.balance == pytest.approx(100000.0)
    assert status.open_positions_count == 0
    assert status.state_reconciled, status.state_reconciliation_reason
    assert list((bridge_dir / "pending_signals").iterdir()) == []


def test_future_broker_close_time_is_clamped_to_now():
    """EA builds before 2026.11.03-3 wrote server time labelled as UTC (2-3 h ahead)."""
    from datetime import timedelta
    from types import SimpleNamespace

    import main

    holder = SimpleNamespace(_latest_broker_close_time_utc=None)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    main.Engine._record_broker_close_time(holder, future.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert holder._latest_broker_close_time_utc <= datetime.now(timezone.utc)
    fresh_snapshot = {"timestamp": datetime.now(timezone.utc).isoformat()}
    assert not main.Engine._is_snapshot_stale_after_broker_close(holder, fresh_snapshot)
