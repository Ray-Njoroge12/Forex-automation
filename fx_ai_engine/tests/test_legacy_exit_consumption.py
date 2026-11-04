"""Consume the GBPUSD exit written by EA build 2026.11.03-2 (server time labelled Z) (STATUS issue 2).

The fixture is a verbatim copy of the live ``exits/exit_59159851261.json``; the
deals are the live ``market/deals.json`` rows for that position. Everything
runs on a temporary bridge and database.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from tests.engine_harness import make_file_bridge_engine
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)
from tests.test_file_bridge_connection import build_bridge

FIXTURE = Path(__file__).parent / "fixtures" / "exits" / "exit_59159851261.json"
TRADE_ID = "AI_20261103_163004_0564f3"
POSITION = 59159851261
LIVE_DEALS = [
    {"ticket": 58760169499, "order": 59161723978, "position_id": POSITION, "time_msc": 1793732596520, "type": 1,
     "entry": 1, "symbol": "GBPUSD", "volume": 23.53, "price": 1.34242, "profit": 1576.51, "commission": 0.0,
     "swap": 0.0, "fee": 0.0, "magic": 0, "comment": ""},
    {"ticket": 58758348702, "order": POSITION, "position_id": POSITION, "time_msc": 1793723405422, "type": 0,
     "entry": 0, "symbol": "GBPUSD", "volume": 23.53, "price": 1.34175, "profit": 0.0, "commission": 0.0,
     "swap": 0.0, "fee": 0.0, "magic": 20261103, "comment": TRADE_ID},
]


def _bridge(tmp_path: Path, *, with_deals: bool = True, payload: dict | None = None) -> Path:
    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    if with_deals:
        deals_path = bridge_dir / "market" / "deals.json"
        deals = json.loads(deals_path.read_text())
        deals["deals"].extend(LIVE_DEALS)
        deals_path.write_text(json.dumps(deals))
    target = bridge_dir / "exits" / FIXTURE.name
    if payload is None:
        shutil.copyfile(FIXTURE, target)
    else:
        target.write_text(json.dumps(payload))
    return bridge_dir


def _seed_trade(connection, evidence) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO trades (trade_id, trade_ticket, position_ticket, evidence_stream, policy_mode,
                   execution_mode, account_scope, symbol, direction, lot_size, entry_price, stop_loss,
                   take_profit, risk_percent, status, execution_time)
               VALUES (?, 58758348702, ?, ?, ?, ?, ?, 'GBPUSD', 'BUY', 23.53, 1.34175, 13.6, 29.92, 0.032,
                       'EXECUTED_OPEN', '2026-11-03T16:30:05Z')""",
            (TRADE_ID, POSITION, evidence.evidence_stream, evidence.policy_mode, evidence.execution_mode,
             evidence.account_scope),
        )
        conn.execute(
            """INSERT INTO trade_reservations (reservation_id, trade_id, symbol, risk_percent, policy_hash, state,
                   reason_code, created_at, updated_at, expires_at)
               VALUES ('RSV_e2d4320c40f141d786f991176419ae60', ?, 'GBPUSD', 0.032, 'test', 'EXECUTED',
                       'EXECUTED', '2026-11-03T16:30:04Z', '2026-11-03T16:30:05Z', '2026-11-03T16:40:04Z')""",
            (TRADE_ID,),
        )


def _trade(connection):
    with connection() as conn:
        return dict(conn.execute("SELECT * FROM trades WHERE trade_id=?", (TRADE_ID,)).fetchone())


def _reservation_state(connection) -> str:
    with connection() as conn:
        return conn.execute("SELECT state FROM trade_reservations WHERE trade_id=?", (TRADE_ID,)).fetchone()[0]


def _exit_receipts(connection) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM feedback_receipts WHERE feedback_kind='exit'").fetchall()
    return [dict(r) for r in rows]


def _consume(tmp_path, monkeypatch, connection, **bridge_kwargs):
    bridge_dir = _bridge(tmp_path, **bridge_kwargs)
    engine = make_file_bridge_engine(bridge_dir, monkeypatch)
    _seed_trade(connection, engine.evidence_context)
    engine._consume_feedback()
    return engine, bridge_dir


def test_fixture_is_the_live_build_2_payload():
    payload = json.loads(FIXTURE.read_text())
    assert payload["close_time"] == "2026-11-03T21:03:16Z"
    assert "close_time_server" not in payload


def test_legacy_exit_closes_trade_with_deal_history_close_time(tmp_path, monkeypatch, isolated_db):
    engine, bridge_dir = _consume(tmp_path, monkeypatch, isolated_db)
    trade = _trade(isolated_db)
    assert trade["status"] == "CLOSED_WIN"
    assert trade["profit_loss"] == pytest.approx(1576.51)
    assert trade["r_multiple"] == pytest.approx(0.4926)
    assert trade["close_time"] == "2026-11-03T19:03:16.520000+00:00"
    assert _reservation_state(isolated_db) == "CLOSED"
    receipts = _exit_receipts(isolated_db)
    assert len(receipts) == 1
    assert receipts[0]["close_time"] == "2026-11-03T19:03:16.520000+00:00"
    assert receipts[0]["dedupe_key"] == main._exit_feedback_dedupe_key(json.loads(FIXTURE.read_text()))
    assert list((bridge_dir / "exits").glob("exit_*.json")) == []
    assert engine._latest_broker_close_time_utc == datetime(2026, 11, 3, 19, 3, 16, 520000, tzinfo=timezone.utc)


def test_legacy_exit_without_deals_subtracts_server_offset(tmp_path, monkeypatch, isolated_db):
    _consume(tmp_path, monkeypatch, isolated_db, with_deals=False)
    trade = _trade(isolated_db)
    assert trade["status"] == "CLOSED_WIN"
    assert trade["close_time"] == "2026-11-03T19:03:16+00:00"
    assert _reservation_state(isolated_db) == "CLOSED"


def test_exit_with_close_time_server_is_left_unchanged(tmp_path, monkeypatch, isolated_db):
    payload = json.loads(FIXTURE.read_text())
    payload.update(close_time="2026-11-03T19:03:16Z", close_time_server="2026-11-03T21:03:16")
    _consume(tmp_path, monkeypatch, isolated_db, payload=payload)
    assert _trade(isolated_db)["close_time"] == "2026-11-03T19:03:16Z"


def test_consuming_the_same_exit_twice_is_idempotent(tmp_path, monkeypatch, isolated_db):
    engine, bridge_dir = _consume(tmp_path, monkeypatch, isolated_db)
    first = _trade(isolated_db)
    shutil.copyfile(FIXTURE, bridge_dir / "exits" / FIXTURE.name)  # EA re-delivers the raw file
    engine._consume_feedback()
    assert list((bridge_dir / "exits").glob("exit_*.json")) == []
    assert _trade(isolated_db) == first
    assert len(_exit_receipts(isolated_db)) == 1


def test_normalisation_only_applies_to_the_file_bridge():
    payload = json.loads(FIXTURE.read_text())
    assert main._normalise_legacy_exit_close_time(payload, SimpleNamespace()) == payload
    no_offset = SimpleNamespace(server_utc_offset_seconds=lambda: None,
                                get_position_history_summary=lambda _ticket: None)
    assert main._normalise_legacy_exit_close_time(payload, no_offset)["close_time"] == payload["close_time"]
