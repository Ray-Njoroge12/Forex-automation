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


@pytest.mark.parametrize("outcome,status", [("TP", "CLOSED_WIN"), ("SL", "CLOSED_LOSS")])
def test_cold_start_with_a_stale_snapshot_reconciles_the_exit_through_main(
    tmp_path, monkeypatch, isolated_db, outcome, status
):
    """HT-009: the engine *starts* after downtime while the snapshot is still stale.
    main() must not exit before reconciliation: the broker-side close is applied exactly
    once, no entry is routed, and a later fresh start records the host recovery."""
    import main
    from application import host_tolerance
    from core.evidence import build_runtime_evidence_context
    from config_microcapital import get_policy_config

    now = datetime.now(timezone.utc)
    bridge_dir, closed, pnl = _bridge(tmp_path, now, outcome=outcome)
    _age_snapshot(bridge_dir, 45)
    monkeypatch.delenv("USE_MT5_MOCK", raising=False)
    monkeypatch.setenv("MT5_CONNECTION", "file_bridge")
    monkeypatch.setenv("BRIDGE_BASE_PATH", str(bridge_dir))
    monkeypatch.setattr(main, "update_health_status", lambda **_: None, raising=False)
    monkeypatch.setattr(main, "start_health_api", lambda *a, **k: None)
    run = _fake_runner()
    monkeypatch.setattr(host_tolerance, "build_host_guard", lambda **kw: HostGuard(
        lambda t: host_rules.guard_checks(run, kw["bridge_path"], t), in_trading_window=lambda t: False))
    from core.file_bridge_connection import FileBridgeConnection
    probe = FileBridgeConnection(bridge_dir)
    assert probe.connect(retry_count=1), probe.last_error
    evidence = build_runtime_evidence_context(get_policy_config(mode_id="core_srs", env={}), use_mock=False,
                                              login=probe.login, server=probe.server)
    _seed_open_trade(isolated_db, evidence, now - timedelta(hours=9))

    assert main.main(["--mode", "demo", "--iterations", "1"]) == 0

    state = _state(isolated_db)
    assert state["trade"]["status"] == status and state["reservation"] == "CLOSED" and state["receipts"] == 1
    assert state["trade"]["profit_loss"] == pytest.approx(pnl)
    assert state["trades"] == 1
    assert list((bridge_dir / "pending_signals").iterdir()) == []
    assert list((bridge_dir / "exits").glob("exit_*.json")) == []

    _age_snapshot(bridge_dir, 0)
    assert main.main(["--mode", "demo", "--iterations", "1"]) == 0
    after = _state(isolated_db)
    assert after["trade"] == state["trade"] and after["receipts"] == 1
    with isolated_db() as conn:
        rules = [r[0] for r in conn.execute("SELECT rule_name FROM risk_events WHERE rule_name LIKE 'HOST_GUARD%'")]
    assert rules == ["HOST_GUARD_BLOCK", "HOST_GUARD_RECOVERED"]


def _main_on_file_bridge(
    tmp_path, monkeypatch, isolated_db, *, outcome: str, mode_id: str, snapshot_age: float, seed_metric: bool = True
):
    """A through-main start on a temporary file bridge with one open trade seeded under mode_id."""
    import main
    from application import host_tolerance
    from core.evidence import build_runtime_evidence_context
    from config_microcapital import ALLOW_NON_SRS_POLICY_ENV, get_policy_config
    from core.file_bridge_connection import FileBridgeConnection

    now = datetime.now(timezone.utc)
    bridge_dir, closed, pnl = _bridge(tmp_path, now, outcome=outcome)
    _age_snapshot(bridge_dir, snapshot_age)
    monkeypatch.delenv("USE_MT5_MOCK", raising=False)
    # main() writes FX_POLICY_MODE into os.environ; set it here so monkeypatch restores it.
    monkeypatch.setenv("FX_POLICY_MODE", mode_id)
    monkeypatch.delenv(main.PRESERVE_10_COMMISSION_PER_LOT_ENV, raising=False)
    monkeypatch.setenv(ALLOW_NON_SRS_POLICY_ENV, "1")
    monkeypatch.setenv("MT5_CONNECTION", "file_bridge")
    monkeypatch.setenv("BRIDGE_BASE_PATH", str(bridge_dir))
    monkeypatch.setattr(main, "update_health_status", lambda **_: None, raising=False)
    monkeypatch.setattr(main, "start_health_api", lambda *a, **k: None)
    run = _fake_runner()
    monkeypatch.setattr(host_tolerance, "build_host_guard", lambda **kw: HostGuard(
        lambda t: host_rules.guard_checks(run, kw["bridge_path"], t), in_trading_window=lambda t: False))
    cycles: list[str] = []
    monkeypatch.setattr(main.decision_cycle, "run_decision_cycle", lambda engine: cycles.append(engine.policy["MODE_ID"]))
    probe = FileBridgeConnection(bridge_dir)
    assert probe.connect(retry_count=1), probe.last_error
    policy = get_policy_config(mode_id=mode_id, env={ALLOW_NON_SRS_POLICY_ENV: "1"})
    evidence = build_runtime_evidence_context(policy, use_mock=False, login=probe.login, server=probe.server)
    assert evidence.policy_mode != "core_srs"
    _seed_open_trade(isolated_db, evidence, now - timedelta(hours=9))
    if seed_metric:
        _seed_account_metric(evidence, now - timedelta(hours=9), consecutive_losses=1)
    return main, bridge_dir, pnl, cycles


def _seed_account_metric(evidence, at: datetime, *, consecutive_losses: int) -> None:
    """The last account metric the engine that opened the trade persisted."""
    from core.account_status import AccountStatus
    from database.repositories.sqlite import SqliteEngineRepository

    SqliteEngineRepository().insert_account_metrics(
        AccountStatus(balance=100000.0, equity=100000.0, peak_equity=100000.0,
                      consecutive_losses=consecutive_losses, is_trading_halted=False, updated_at=at),
        evidence_context=evidence,
    )


def _metrics(connection) -> list[dict]:
    with connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT balance, equity, consecutive_losses, is_trading_halted FROM account_metrics ORDER BY id")]


@pytest.mark.parametrize("mode_id", ["legacy_micro_capital", "preserve_10", "micro_live"])
@pytest.mark.parametrize("outcome,status", [("TP", "CLOSED_WIN"), ("SL", "CLOSED_LOSS")])
def test_non_srs_cold_start_with_a_stale_snapshot_reconciles_the_exit_before_the_policy_gate_refusal(
    tmp_path, monkeypatch, isolated_db, mode_id, outcome, status
):
    """HT-009A: a non-SRS demo start on a stale snapshot is refused by the policy gate
    (account unverified, exit code 4), yet the broker-side close is applied exactly once,
    the reservation is released, no decision cycle runs and nothing is routed."""
    main, bridge_dir, pnl, cycles = _main_on_file_bridge(
        tmp_path, monkeypatch, isolated_db, outcome=outcome, mode_id=mode_id, snapshot_age=45
    )

    assert main.main(["--mode", "demo", "--iterations", "1", "--policy-mode", mode_id]) == 4

    state = _state(isolated_db)
    assert state["trade"]["status"] == status and state["reservation"] == "CLOSED" and state["receipts"] == 1
    assert state["trade"]["profit_loss"] == pytest.approx(pnl)
    assert state["trades"] == 1
    assert cycles == []
    assert list((bridge_dir / "pending_signals").iterdir()) == []
    assert list((bridge_dir / "exits").glob("exit_*.json")) == []
    with isolated_db() as conn:
        blocks = [r[0] for r in conn.execute(
            "SELECT reason FROM risk_events WHERE rule_name='NON_SRS_POLICY_APPROVAL' AND severity='BLOCK'")]
    assert len(blocks) == 1 and "NON_SRS_POLICY_REQUIRES_MOCK_OR_DEMO" in blocks[0]
    # HT-009A-1: one truthful loss-streak checkpoint, no zero-balance or forced-halt row.
    expected_losses = 2 if outcome == "SL" else 0
    assert _metrics(isolated_db) == [
        {"balance": 100000.0, "equity": 100000.0, "consecutive_losses": 1, "is_trading_halted": 0},
        {"balance": 100000.0, "equity": 100000.0, "consecutive_losses": expected_losses, "is_trading_halted": 0},
    ]

    # Refused again (still stale): nothing is applied twice.
    assert main.main(["--mode", "demo", "--iterations", "1", "--policy-mode", mode_id]) == 4
    again = _state(isolated_db)
    assert again["trade"] == state["trade"] and again["receipts"] == 1 and again["recovered"] == 0
    assert cycles == [] and list((bridge_dir / "pending_signals").iterdir()) == []
    assert len(_metrics(isolated_db)) == 2


@pytest.mark.parametrize("outcome,status", [("TP", "CLOSED_WIN"), ("SL", "CLOSED_LOSS")])
def test_preserve_10_startup_refusal_on_a_verified_demo_still_reconciles_the_exit(
    tmp_path, monkeypatch, isolated_db, outcome, status
):
    """HT-009A: the Preserve-$10 startup gate (exit code 3) is the other pre-Engine refusal.
    The account is a verified demo, but the commission evidence is missing; the close is
    still applied once and nothing is routed."""
    main, bridge_dir, pnl, cycles = _main_on_file_bridge(
        tmp_path, monkeypatch, isolated_db, outcome=outcome, mode_id="preserve_10", snapshot_age=0
    )

    assert main.main(["--mode", "demo", "--iterations", "1", "--policy-mode", "preserve_10"]) == 3

    state = _state(isolated_db)
    assert state["trade"]["status"] == status and state["reservation"] == "CLOSED" and state["receipts"] == 1
    assert state["trade"]["profit_loss"] == pytest.approx(pnl)
    assert cycles == []
    assert list((bridge_dir / "pending_signals").iterdir()) == []
    assert list((bridge_dir / "exits").glob("exit_*.json")) == []
    with isolated_db() as conn:
        refusals = [r[0] for r in conn.execute(
            "SELECT reason FROM risk_events WHERE rule_name='PRESERVE_10_STARTUP_APPROVAL' AND severity='BLOCK'")]
    assert len(refusals) == 1
    metrics = _metrics(isolated_db)
    assert len(metrics) == 2 and all(m["balance"] == 100000.0 and m["is_trading_halted"] == 0 for m in metrics)
    assert metrics[-1]["consecutive_losses"] == (2 if outcome == "SL" else 0)


def test_refused_start_without_a_prior_account_metric_defers_the_loss_streak_and_writes_no_metric(
    tmp_path, monkeypatch, isolated_db
):
    """HT-009A-1: with no metric to copy, nothing synthetic is persisted; a BLOCK records the deferral."""
    main, _bridge_dir, _pnl, cycles = _main_on_file_bridge(
        tmp_path, monkeypatch, isolated_db, outcome="SL", mode_id="legacy_micro_capital", snapshot_age=45,
        seed_metric=False,
    )

    assert main.main(["--mode", "demo", "--iterations", "1", "--policy-mode", "legacy_micro_capital"]) == 4

    assert _state(isolated_db)["trade"]["status"] == "CLOSED_LOSS" and cycles == []
    assert _metrics(isolated_db) == []
    with isolated_db() as conn:
        deferred = [r[0] for r in conn.execute(
            "SELECT reason FROM risk_events WHERE rule_name='LOSS_STREAK_CHECKPOINT_DEFERRED' AND severity='BLOCK'")]
    assert len(deferred) == 1 and "consecutive_losses=1" in deferred[0]


@pytest.mark.parametrize("outcome,losses", [("TP", 0), ("SL", 2)])
def test_restart_with_a_waiting_exit_writes_no_zero_balance_metric(tmp_path, monkeypatch, isolated_db, outcome, losses):
    """HT-009A-1 in the normal loop: the exit is applied before the first account update,
    so its loss-streak checkpoint copies the last persisted metric instead of the defaults."""
    engine, _bridge_dir, _closed, _pnl, _cycles = _restart(tmp_path, monkeypatch, isolated_db, outcome=outcome)
    _seed_account_metric(engine.evidence_context, datetime.now(timezone.utc) - timedelta(hours=9), consecutive_losses=1)
    engine._consecutive_losses_seeded = False  # the engine was built before the metric; a real restart reads it

    engine.run(mode="demo", iterations=1)

    metrics = _metrics(isolated_db)
    assert all(m["balance"] > 0 and m["equity"] > 0 for m in metrics), metrics
    assert metrics[1] == {"balance": 100000.0, "equity": 100000.0, "consecutive_losses": losses, "is_trading_halted": 0}
    assert metrics[-1]["consecutive_losses"] == losses
    assert engine.account_status.consecutive_losses == losses


def test_reconciliation_only_engine_never_runs_the_loop_or_a_decision_cycle(tmp_path, monkeypatch, isolated_db):
    import main

    engine, bridge_dir, _closed, _pnl, _cycles = _restart(tmp_path, monkeypatch, isolated_db, outcome="TP")
    del engine._decision_cycle  # use the real method, not the harness probe
    engine.reconcile_only = True
    routed: list[object] = []
    monkeypatch.setattr(main.decision_cycle, "run_decision_cycle", lambda e: routed.append(e))

    with pytest.raises(RuntimeError, match="reconciliation-only"):
        engine.run(mode="demo", iterations=1)
    engine._decision_cycle()
    engine.reconcile_once()

    assert routed == []
    assert engine.account_status.is_trading_halted
    assert _state(isolated_db)["trade"]["status"] == "CLOSED_WIN"
    assert list((bridge_dir / "pending_signals").iterdir()) == []
