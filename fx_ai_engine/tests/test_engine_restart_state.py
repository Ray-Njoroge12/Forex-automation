"""An engine restart mid-bar must not re-evaluate that bar (STATUS issue 5)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from database import db
from tests.engine_harness import make_file_bridge_engine
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)
from tests.test_file_bridge_connection import build_bridge

STATE_KEY = "last_m15_candle_by_symbol"


def test_restarted_engine_does_not_rerun_the_current_bar(tmp_path, monkeypatch, isolated_db):
    now = datetime.now(timezone.utc)
    clock = {"now": now}
    bridge_dir = build_bridge(tmp_path / "bridge", now, open_tickets=())
    first = make_file_bridge_engine(bridge_dir, monkeypatch, clock=lambda: clock["now"])
    assert first._is_new_m15_candle() is True

    second = make_file_bridge_engine(bridge_dir, monkeypatch, clock=lambda: clock["now"])
    assert second.last_m15_candle == first.last_m15_candle
    assert second._is_new_m15_candle() is False

    clock["now"] = now + timedelta(minutes=15)
    build_bridge(bridge_dir, clock["now"], open_tickets=())
    assert second._is_new_m15_candle() is True
    stored = json.loads(db.get_engine_runtime_state(STATE_KEY, evidence_context=second.evidence_context))
    assert set(stored) == {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"}


def test_state_is_not_shared_across_account_scopes(tmp_path, monkeypatch, isolated_db):
    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    first = make_file_bridge_engine(bridge_dir, monkeypatch)
    assert first._is_new_m15_candle() is True
    for path in (bridge_dir / "market" / "status.json", bridge_dir / "feedback" / "account_snapshot.json"):
        payload = json.loads(path.read_text())
        payload["login"] = 123456
        path.write_text(json.dumps(payload))
    other = make_file_bridge_engine(bridge_dir, monkeypatch)
    assert other.evidence_context.account_scope != first.evidence_context.account_scope
    assert other._is_new_m15_candle() is True


def test_malformed_or_future_stored_state_is_ignored(tmp_path, monkeypatch, isolated_db):
    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    probe = make_file_bridge_engine(bridge_dir, monkeypatch)
    db.set_engine_runtime_state(STATE_KEY, "{not json", evidence_context=probe.evidence_context)
    assert make_file_bridge_engine(bridge_dir, monkeypatch)._is_new_m15_candle() is True

    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    db.set_engine_runtime_state(STATE_KEY, json.dumps({"EURUSD": future, "XAUUSD": future}),
                                evidence_context=probe.evidence_context)
    engine = make_file_bridge_engine(bridge_dir, monkeypatch)
    assert engine._last_m15_candle_by_symbol == {}
    assert engine._is_new_m15_candle() is True


def test_engine_runtime_state_migration_is_idempotent(isolated_db):
    db.run_all_migrations()
    db.run_all_migrations()
    with isolated_db() as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='engine_runtime_state'"
        ).fetchone() is not None
