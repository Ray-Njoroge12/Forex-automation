"""Restart after about 8 hours of engine downtime with a broker-side TP or SL close (backlog item 5).

The EA kept running (or swept the close on its own restart), so the bridge holds
the final exit file and the deal history for the position. On the first engine
loop the trade must reconcile to closed exactly once, before any decision cycle
can route a new entry; a second loop must not close it again. Everything runs
on a temporary bridge and database.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from application.host_tolerance import HostGuard
from core import host_rules
from core.host_rules import BLOCK, Check
from tests.engine_harness import make_file_bridge_engine
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)
from tests.test_file_bridge_connection import build_bridge
from tests.test_host_check import _fake_runner

TRADE_ID = "AI_20261104_070004_d0wn01"
POSITION = 777001
DEAL_IN = 777002
MAGIC = 20261103


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _bridge(tmp_path, now: datetime, *, outcome: str):
    opened = now - timedelta(hours=9)
    closed = now - timedelta(hours=5)  # broker closed it three hours into an 8-hour outage
    win = outcome == "TP"
    pnl, r_multiple, close_price = (220.0, 2.2, 1.10220) if win else (-100.0, -1.0, 1.09900)
    bridge_dir = build_bridge(tmp_path / "bridge", now, open_tickets=())
    deals_path = bridge_dir / "market" / "deals.json"
    deals = json.loads(deals_path.read_text())
    deals["deals"] += [
        {"ticket": 777003, "order": 777004, "position_id": POSITION, "time_msc": int(closed.timestamp() * 1000),
         "type": 1, "entry": 1, "symbol": "EURUSD", "volume": 1.0, "price": close_price, "profit": pnl,
         "commission": 0.0, "swap": 0.0, "fee": 0.0, "magic": MAGIC, "comment": "[tp]" if win else "[sl]"},
        {"ticket": DEAL_IN, "order": POSITION, "position_id": POSITION, "time_msc": int(opened.timestamp() * 1000),
         "type": 0, "entry": 0, "symbol": "EURUSD", "volume": 1.0, "price": 1.10000, "profit": 0.0,
         "commission": 0.0, "swap": 0.0, "fee": 0.0, "magic": MAGIC, "comment": TRADE_ID},
    ]
    deals_path.write_text(json.dumps(deals))
    server_close = closed + timedelta(hours=2)
    (bridge_dir / "exits" / f"exit_{POSITION}.json").write_text(json.dumps({
        "ticket": POSITION, "position_ticket": POSITION, "trade_id": TRADE_ID, "profit_loss": pnl,
        "status": "CLOSED_WIN" if win else "CLOSED_LOSS", "is_final_exit": True, "r_multiple": r_multiple,
        "close_time": _iso(closed), "close_time_server": server_close.strftime("%Y-%m-%dT%H:%M:%S"),
    }))
    return bridge_dir, closed, pnl


def _seed_open_trade(connection, evidence, opened: datetime) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO trades (trade_id, trade_ticket, position_ticket, evidence_stream, policy_mode,
                   execution_mode, account_scope, symbol, direction, lot_size, entry_price, stop_loss,
                   take_profit, risk_percent, status, execution_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'EURUSD', 'BUY', 1.0, 1.10000, 10.0, 22.0, 0.032,
                       'EXECUTED_OPEN', ?)""",
            (TRADE_ID, DEAL_IN, POSITION, evidence.evidence_stream, evidence.policy_mode,
             evidence.execution_mode, evidence.account_scope, _iso(opened)),
        )
        conn.execute(
            """INSERT INTO trade_reservations (reservation_id, trade_id, symbol, risk_percent, policy_hash, state,
                   reason_code, created_at, updated_at, expires_at)
               VALUES ('RSV_downtime_0001', ?, 'EURUSD', 0.032, 'test', 'EXECUTED', 'EXECUTED', ?, ?, ?)""",
            (TRADE_ID, _iso(opened), _iso(opened), _iso(opened + timedelta(minutes=10))),
        )


def _state(connection) -> dict:
    with connection() as conn:
        trade = dict(conn.execute("SELECT * FROM trades WHERE trade_id=?", (TRADE_ID,)).fetchone())
        reservation = conn.execute("SELECT state FROM trade_reservations WHERE trade_id=?", (TRADE_ID,)).fetchone()[0]
        receipts = conn.execute(
            "SELECT COUNT(*) FROM feedback_receipts WHERE feedback_kind='exit' AND trade_id=?", (TRADE_ID,)
        ).fetchone()[0]
        recovered = conn.execute(
            "SELECT COUNT(*) FROM risk_events WHERE rule_name='TRADE_LIFECYCLE_RECOVERED'"
        ).fetchone()[0]
        trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    return {"trade": trade, "reservation": reservation, "receipts": receipts, "recovered": recovered, "trades": trades}


def _restart(tmp_path, monkeypatch, connection, *, outcome: str, host_guard: HostGuard | None = None):
    now = datetime.now(timezone.utc)
    bridge_dir, closed, pnl = _bridge(tmp_path, now, outcome=outcome)
    engine = make_file_bridge_engine(bridge_dir, monkeypatch)
    if host_guard is not None:
        engine.host_guard = host_guard
    _seed_open_trade(connection, engine.evidence_context, now - timedelta(hours=9))
    cycles: list[dict] = []

    def decision_cycle_probe() -> None:
        # Stands in for the decision cycle: records what an entry decision would see.
        cycles.append(_state(connection) | {"pending": sorted(p.name for p in (bridge_dir / "pending_signals").iterdir())})

    engine._decision_cycle = decision_cycle_probe
    return engine, bridge_dir, closed, pnl, cycles


@pytest.mark.parametrize("outcome,status", [("TP", "CLOSED_WIN"), ("SL", "CLOSED_LOSS")])
def test_broker_side_close_during_8h_downtime_reconciles_once_before_any_entry(
    tmp_path, monkeypatch, isolated_db, outcome, status
):
    engine, bridge_dir, closed, pnl, cycles = _restart(tmp_path, monkeypatch, isolated_db, outcome=outcome)
    assert _state(isolated_db)["trade"]["status"] == "EXECUTED_OPEN"

    engine.run(mode="demo", iterations=1)  # first loop after the restart

    # A new M15 bar was waiting after 8 hours, and the decision cycle ran only after the close was applied.
    assert len(cycles) == 1
    seen = cycles[0]
    assert seen["trade"]["status"] == status
    assert seen["reservation"] == "CLOSED"
    assert seen["receipts"] == 1
    assert seen["pending"] == []
    assert seen["trades"] == 1

    after_first = _state(isolated_db)
    assert after_first["trade"]["profit_loss"] == pytest.approx(pnl)
    assert after_first["trade"]["close_time"] == _iso(closed)
    assert list((bridge_dir / "exits").glob("exit_*.json")) == []
    assert engine.account_status.open_positions_count == 0
    assert engine.account_status.state_reconciled, engine.account_status.state_reconciliation_reason
    assert engine.account_status.consecutive_losses == (1 if outcome == "SL" else 0)

    engine.run(mode="demo", iterations=1)  # deal history still lists the close; nothing changes

    after_second = _state(isolated_db)
    assert after_second["trade"] == after_first["trade"]
    assert after_second["receipts"] == 1
    assert after_second["recovered"] == 0  # the exit file closed it; broker-history recovery had nothing to do
    assert engine.account_status.consecutive_losses == (1 if outcome == "SL" else 0)


def _age_snapshot(bridge_dir, seconds: float) -> None:
    path = bridge_dir / "feedback" / "account_snapshot.json"
    snapshot = json.loads(path.read_text())
    snapshot["timestamp"] = _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))
    path.write_text(json.dumps(snapshot))


@pytest.mark.parametrize("outcome,status", [("TP", "CLOSED_WIN"), ("SL", "CLOSED_LOSS")])
def test_exit_reconciles_while_the_demo_snapshot_is_stale(tmp_path, monkeypatch, isolated_db, outcome, status):
    """HT-007: a stale snapshot leaves the account unverified and blocks the host guard;
    the broker-side close is still applied once, nothing is routed, and the host recovers."""
    holder: dict = {}
    run = _fake_runner()  # power, sleep, App Nap and process probes pass; only the snapshot can fail
    guard = HostGuard(
        lambda now: host_rules.guard_checks(run, holder["bridge"], now),
        blocked_interval_seconds=0,
        in_trading_window=lambda now: False,
    )
    engine, bridge_dir, _closed, _pnl, cycles = _restart(
        tmp_path, monkeypatch, isolated_db, outcome=outcome, host_guard=guard
    )
    holder["bridge"] = bridge_dir
    _age_snapshot(bridge_dir, 45)

    engine.run(mode="demo", iterations=1)

    state = _state(isolated_db)
    assert cycles == []  # no decision cycle while the account is unverified
    assert list((bridge_dir / "pending_signals").iterdir()) == []
    assert state["trade"]["status"] == status and state["reservation"] == "CLOSED" and state["receipts"] == 1
    assert state["trades"] == 1
    assert list((bridge_dir / "exits").glob("exit_*.json")) == []
    assert engine.account_status.is_trading_halted
    assert guard.status.blocked and guard.status.failed == ("bridge_snapshot_fresh",)

    engine.run(mode="demo", iterations=1)  # still stale: nothing is applied twice
    assert _state(isolated_db)["trade"] == state["trade"] and _state(isolated_db)["receipts"] == 1

    _age_snapshot(bridge_dir, 0)
    engine.run(mode="demo", iterations=1)

    after = _state(isolated_db)
    assert after["trade"] == state["trade"] and after["receipts"] == 1 and after["recovered"] == 0
    assert not guard.status.blocked
    assert all(seen["trade"]["status"] == status and seen["pending"] == [] for seen in cycles)
    with isolated_db() as conn:
        rules = [r[0] for r in conn.execute("SELECT rule_name FROM risk_events WHERE rule_name LIKE 'HOST_GUARD%'")]
    assert rules == ["HOST_GUARD_BLOCK", "HOST_GUARD_RECOVERED"]


def test_exit_reconciles_while_the_host_guard_blocks_entries(tmp_path, monkeypatch, isolated_db):
    guard = HostGuard(
        lambda now: [Check("power_source", BLOCK, "power=BATTERY")],
        in_trading_window=lambda now: False,
    )
    engine, bridge_dir, _closed, _pnl, cycles = _restart(
        tmp_path, monkeypatch, isolated_db, outcome="SL", host_guard=guard
    )

    engine.run(mode="demo", iterations=1)

    state = _state(isolated_db)
    assert guard.status.blocked
    assert state["trade"]["status"] == "CLOSED_LOSS" and state["reservation"] == "CLOSED" and state["receipts"] == 1
    assert list((bridge_dir / "exits").glob("exit_*.json")) == []
    with isolated_db() as conn:
        rules = [r[0] for r in conn.execute("SELECT rule_name FROM risk_events WHERE rule_name LIKE 'HOST_GUARD%'")]
    assert rules == ["HOST_GUARD_BLOCK"]
