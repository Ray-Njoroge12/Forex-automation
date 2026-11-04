"""Per-loop halts of the live gate (LiveSession.check_loop and the engine hooks)."""
from __future__ import annotations

import logging
import os
from datetime import timedelta

import pytest

from core import health, live_gate
from core.account_status import AccountStatus
from core.evidence import build_runtime_evidence_context
from tests.live_fixtures import (
    DROP,
    LOGIN,
    MAX_TOTAL_LOSS,
    SERVER,
    STARTING_BALANCE,
    folder_state,
    make_scenario,
    risk_events,
    write_live_guard,
    write_status,
)
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _clean_session():
    live_gate.reset_active_session()
    yield
    live_gate.reset_active_session()


class FakeEngine:
    """Just enough Engine for the live gate: account status, policy, event sink, two hooks."""

    def __init__(self, policy, now, *, equity=STARTING_BALANCE):
        self.policy = policy
        self.use_mock = False
        self.account_status = AccountStatus(balance=equity, equity=equity, updated_at=now)
        self.events: list[tuple[str, str, str]] = []
        self.enforce_calls = 0

    def _insert_risk_event(self, rule_name, severity, reason, trade_id=None):
        self.events.append((rule_name, severity, reason))

    def _enforce_execution_feedback_integrity(self):
        self.enforce_calls += 1

    def _record_consecutive_loss_update(self, pnl):
        if pnl < 0:
            self.account_status.consecutive_losses += 1
        elif pnl > 0:
            self.account_status.consecutive_losses = 0

    def rules(self):
        return [e[0] for e in self.events]


class Loop:
    """A started session on a fake engine, with a helper that mimics one loop pass."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.session = scenario.gate()
        assert self.session is not None
        self.engine = FakeEngine(self.session.policy, scenario.now)
        self.now = scenario.now

    def tick(self, seconds=1, *, refresh_status=True, refresh_equity=True):
        """Advance the clock, refresh the EA files like a running EA, then run the check."""
        self.now = self.now + timedelta(seconds=seconds)
        if refresh_status:
            write_status(self.scenario.bridge_dir, self.now)
        if refresh_equity:
            self.engine.account_status.updated_at = self.now
        # state_sync resets the halt flag at the start of every loop.
        self.engine.account_status.is_trading_halted = False
        return self.session.check_loop(self.engine, now=self.now)


@pytest.fixture
def loop(tmp_path, isolated_db):  # noqa: F811
    return Loop(make_scenario(tmp_path))


def _halted(loop, code, codes):
    assert code in codes, codes
    assert loop.engine.account_status.is_trading_halted is True


# ----------------------------------------------------------------------
# Clean loop and health fields
# ----------------------------------------------------------------------
def test_clean_loop_does_not_halt_and_updates_health(loop):
    assert loop.tick() == []
    assert loop.engine.account_status.is_trading_halted is False
    assert loop.engine.events == []
    snapshot = health._health_status
    assert snapshot["live_mode"] is True
    assert snapshot["live_evidence_waiver"] is False
    assert snapshot["live_halt_reasons"] == []
    assert snapshot["live_approval_expires_at"] == loop.session.approval.expires_at_utc.isoformat()


def test_check_loop_never_writes_to_the_bridge_folder(loop):
    loop.tick()  # refresh the EA files first
    (loop.scenario.bridge_dir / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    before = folder_state(loop.scenario.bridge_dir)
    loop.session.check_loop(loop.engine, now=loop.now)
    assert folder_state(loop.scenario.bridge_dir) == before
    assert list((loop.scenario.bridge_dir / "pending_signals").iterdir()) == []


def test_one_risk_event_per_transition_not_per_loop(loop):
    (loop.scenario.bridge_dir / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    for _ in range(3):
        _halted(loop, "LIVE_KILL_SWITCH", loop.tick())
    assert loop.engine.rules() == ["LIVE_KILL_SWITCH"]
    assert loop.engine.events[0][1] == "BLOCK"


# ----------------------------------------------------------------------
# Approval file
# ----------------------------------------------------------------------
def test_expiry_halts(loop):
    expiry = loop.session.approval.expires_at_utc
    loop.now = expiry - timedelta(seconds=2)
    assert "LIVE_APPROVAL_EXPIRED" not in loop.tick()  # tick() moves the clock to expiry - 1 s
    loop.now = expiry
    codes = loop.tick(0)
    _halted(loop, "LIVE_APPROVAL_EXPIRED", codes)
    assert ("LIVE_APPROVAL_EXPIRED", "CRITICAL") in [(e[0], e[1]) for e in loop.engine.events]


def test_edited_approval_file_halts_and_stays_halted(loop):
    original = loop.scenario.approval_path.read_bytes()
    loop.scenario.rewrite_approval(approved_by="Somebody Else")
    _halted(loop, "LIVE_APPROVAL_CHANGED", loop.tick())
    loop.scenario.approval_path.write_bytes(original)
    _halted(loop, "LIVE_APPROVAL_CHANGED", loop.tick())
    assert loop.engine.rules().count("LIVE_APPROVAL_CHANGED") == 1


def test_approval_edit_that_changes_the_policy_hash_halts(loop):
    loop.scenario.rewrite_approval(policy_hash="f" * 64)
    _halted(loop, "LIVE_APPROVAL_CHANGED", loop.tick())


def test_deleted_approval_file_halts(loop):
    loop.scenario.approval_path.unlink()
    _halted(loop, "LIVE_APPROVAL_CHANGED", loop.tick())


# ----------------------------------------------------------------------
# Kill switch
# ----------------------------------------------------------------------
def test_kill_switch_file_halts_and_removal_clears(loop):
    kill = loop.scenario.bridge_dir / "KILL_SWITCH"
    kill.write_text("stop", encoding="utf-8")
    _halted(loop, "LIVE_KILL_SWITCH", loop.tick())
    kill.unlink()
    assert loop.tick() == []
    assert loop.engine.account_status.is_trading_halted is False
    assert ("LIVE_HALT_CLEARED", "INFO") in [(e[0], e[1]) for e in loop.engine.events]
    assert "LIVE_KILL_SWITCH cleared" in loop.engine.events[-1][2]


def test_kill_switch_stat_error_counts_as_present(loop, monkeypatch):
    real_stat = os.stat

    def flaky(path, *args, **kwargs):
        if str(path).endswith("KILL_SWITCH"):
            raise PermissionError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(live_gate.os, "stat", flaky)
    codes = loop.tick()
    _halted(loop, "LIVE_KILL_SWITCH", codes)
    assert "denied" in loop.engine.events[0][2]


# ----------------------------------------------------------------------
# Loss limit
# ----------------------------------------------------------------------
def test_loss_one_cent_below_the_limit_does_not_halt(loop):
    loop.engine.account_status.equity = STARTING_BALANCE - MAX_TOTAL_LOSS + 0.01
    assert loop.tick() == []


def test_loss_at_the_limit_halts_and_latches(loop):
    loop.engine.account_status.equity = STARTING_BALANCE - MAX_TOTAL_LOSS
    codes = loop.tick()
    _halted(loop, "LIVE_LOSS_LIMIT", codes)
    assert ("LIVE_LOSS_LIMIT", "CRITICAL") in [(e[0], e[1]) for e in loop.engine.events]
    # Equity recovers: the halt stays for the rest of the process.
    loop.engine.account_status.equity = STARTING_BALANCE
    _halted(loop, "LIVE_LOSS_LIMIT", loop.tick())
    assert loop.engine.rules().count("LIVE_LOSS_LIMIT") == 1


def test_loss_beyond_the_limit_halts(loop):
    loop.engine.account_status.equity = 1.0
    _halted(loop, "LIVE_LOSS_LIMIT", loop.tick())


def test_stale_equity_halts_without_latching(loop):
    loop.engine.account_status.equity = 1.0  # would be a loss of 9 USD
    loop.engine.account_status.updated_at = loop.scenario.now - timedelta(seconds=30)
    codes = loop.tick(seconds=120, refresh_equity=False)  # the snapshot is now 150 s old
    _halted(loop, "LIVE_EQUITY_STALE", codes)
    assert "LIVE_LOSS_LIMIT" not in codes
    assert ("LIVE_EQUITY_STALE", "BLOCK") in [(e[0], e[1]) for e in loop.engine.events]
    # Fresh, healthy equity: the stale halt clears and no loss latch was set.
    loop.engine.account_status.equity = STARTING_BALANCE
    assert loop.tick() == []
    assert "LIVE_LOSS_LIMIT" not in loop.engine.rules()


def test_default_zero_equity_before_the_first_snapshot_does_not_latch(loop):
    loop.engine.account_status.equity = 0.0
    codes = loop.tick()
    _halted(loop, "LIVE_EQUITY_STALE", codes)
    assert "LIVE_LOSS_LIMIT" not in codes
    loop.engine.account_status.equity = STARTING_BALANCE
    assert loop.tick() == []


# ----------------------------------------------------------------------
# EA status, identity, live_guard
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "changes",
    [
        {"account_real": False},
        {"allow_live": False},
        {"kill_switch": True},
        {"max_lot": 0.02},
        {"max_orders_per_day": 9},
        {"max_lot": DROP},
    ],
)
def test_live_guard_mismatch_halts(loop, changes):
    write_live_guard(loop.scenario.bridge_dir, loop.now, **changes)
    codes = loop.tick()
    _halted(loop, "LIVE_GUARD_MISMATCH", codes)
    assert ("LIVE_GUARD_MISMATCH", "CRITICAL") in [(e[0], e[1]) for e in loop.engine.events]


def test_missing_live_guard_block_halts(loop):
    write_status(loop.scenario.bridge_dir, loop.now, live_guard=DROP)
    _halted(loop, "LIVE_GUARD_MISMATCH", loop.tick(refresh_status=False))


def test_ea_downgrade_halts(loop):
    write_status(loop.scenario.bridge_dir, loop.now, ea_version="2026.11.04-1")
    _halted(loop, "LIVE_GUARD_MISMATCH", loop.tick(refresh_status=False))


def test_stale_ea_status_halts(loop):
    codes = loop.tick(seconds=60, refresh_status=False)
    _halted(loop, "LIVE_EA_STATUS_UNAVAILABLE", codes)
    assert ("LIVE_EA_STATUS_UNAVAILABLE", "BLOCK") in [(e[0], e[1]) for e in loop.engine.events]
    assert loop.tick() == []  # EA is back: cleared


def test_missing_ea_status_halts(loop):
    (loop.scenario.bridge_dir / "market" / "status.json").unlink()
    _halted(loop, "LIVE_EA_STATUS_UNAVAILABLE", loop.tick(refresh_status=False))


def test_malformed_ea_status_halts(loop):
    (loop.scenario.bridge_dir / "market" / "status.json").write_text("[]", encoding="utf-8")
    _halted(loop, "LIVE_EA_STATUS_UNAVAILABLE", loop.tick(refresh_status=False))


@pytest.mark.parametrize(
    "changes",
    [{"login": LOGIN + 1}, {"server": "Other-Server"}, {"account_is_demo": True}, {"account_is_demo": DROP}],
)
def test_account_change_halts(loop, changes):
    write_status(loop.scenario.bridge_dir, loop.now, **changes)
    codes = loop.tick(refresh_status=False)
    _halted(loop, "LIVE_ACCOUNT_CHANGED", codes)
    assert ("LIVE_ACCOUNT_CHANGED", "CRITICAL") in [(e[0], e[1]) for e in loop.engine.events]


# ----------------------------------------------------------------------
# Loss-streak latch
# ----------------------------------------------------------------------
def _latch_row(isolated_db, session):  # noqa: F811
    with isolated_db() as conn:
        return conn.execute(
            "SELECT value FROM engine_runtime_state WHERE key=? AND account_scope=?",
            (live_gate.LOSS_LATCH_KEY, session.evidence_context.account_scope),
        ).fetchone()


def test_attach_wraps_the_two_hooks_once(loop):
    engine = loop.engine
    assert loop.session.attach(engine) is True
    assert loop.session.attach(engine) is True  # idempotent
    engine._enforce_execution_feedback_integrity()
    assert engine.enforce_calls == 1
    # The wrapped hook ran the original and then the live check, which found nothing wrong.
    assert engine.account_status.is_trading_halted is False
    assert engine.events == []
    assert health._health_status["live_halt_reasons"] == []


def test_wrapped_hook_halts_through_the_engine_slot(loop):
    engine = loop.engine
    assert loop.session.attach(engine)
    engine.account_status.updated_at = loop.now
    (loop.scenario.bridge_dir / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    engine.account_status.is_trading_halted = False
    engine._enforce_execution_feedback_integrity()
    assert engine.enforce_calls == 1
    assert engine.account_status.is_trading_halted is True


def test_three_losses_latch_and_a_win_does_not_clear_it(loop, isolated_db):  # noqa: F811
    engine = loop.engine
    assert loop.session.attach(engine)
    for _ in range(2):
        engine._record_consecutive_loss_update(-1.0)
    assert _latch_row(isolated_db, loop.session) is None
    assert loop.tick() == []
    engine._record_consecutive_loss_update(-1.0)
    assert _latch_row(isolated_db, loop.session) is not None
    assert engine.account_status.is_trading_halted is True
    engine._record_consecutive_loss_update(1.0)  # a win resets the counter
    assert engine.account_status.consecutive_losses == 0
    codes = loop.tick()
    _halted(loop, "LIVE_LOSS_STREAK_LATCHED", codes)
    assert ("LIVE_LOSS_STREAK_LATCHED", "CRITICAL") in [(e[0], e[1]) for e in loop.engine.events]


def test_loss_then_win_in_one_pass_still_latches(loop, isolated_db):  # noqa: F811
    engine = loop.engine
    assert loop.session.attach(engine)
    engine.account_status.consecutive_losses = 2
    engine._record_consecutive_loss_update(-0.5)
    engine._record_consecutive_loss_update(0.5)
    assert engine.account_status.consecutive_losses == 0
    _halted(loop, "LIVE_LOSS_STREAK_LATCHED", loop.tick())


def test_latch_survives_a_new_session(loop, isolated_db):  # noqa: F811
    engine = loop.engine
    assert loop.session.attach(engine)
    engine.account_status.consecutive_losses = 2
    engine._record_consecutive_loss_update(-1.0)
    live_gate.reset_active_session()
    restarted = Loop(loop.scenario)  # same scenario, same database: a restart
    assert restarted.engine.account_status.consecutive_losses == 0
    _halted(restarted, "LIVE_LOSS_STREAK_LATCHED", restarted.tick())


def test_latch_clears_only_when_the_key_is_removed(loop, isolated_db):  # noqa: F811
    engine = loop.engine
    assert loop.session.attach(engine)
    engine.account_status.consecutive_losses = 2
    engine._record_consecutive_loss_update(-1.0)
    engine.account_status.consecutive_losses = 0
    _halted(loop, "LIVE_LOSS_STREAK_LATCHED", loop.tick())
    with isolated_db() as conn:
        conn.execute("DELETE FROM engine_runtime_state WHERE key=?", (live_gate.LOSS_LATCH_KEY,))
    assert loop.tick() == []
    assert "LIVE_HALT_CLEARED" in loop.engine.rules()


def test_latch_is_scoped_to_the_account(loop, isolated_db):  # noqa: F811
    engine = loop.engine
    assert loop.session.attach(engine)
    engine.account_status.consecutive_losses = 2
    engine._record_consecutive_loss_update(-1.0)
    other = build_runtime_evidence_context(loop.session.policy, use_mock=False, login=LOGIN + 1, server=SERVER)
    assert live_gate.db.get_engine_runtime_state(live_gate.LOSS_LATCH_KEY, evidence_context=other) is None


def test_latch_write_failure_still_halts(loop, monkeypatch):
    engine = loop.engine
    assert loop.session.attach(engine)
    monkeypatch.setattr(live_gate.db, "set_engine_runtime_state", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    engine.account_status.consecutive_losses = 2
    engine._record_consecutive_loss_update(-1.0)
    engine._record_consecutive_loss_update(1.0)
    _halted(loop, "LIVE_LOSS_STREAK_LATCHED", loop.tick())


def test_streak_already_at_threshold_on_startup_latches(loop, isolated_db):  # noqa: F811
    loop.engine.account_status.consecutive_losses = 3
    _halted(loop, "LIVE_LOSS_STREAK_LATCHED", loop.tick())
    assert _latch_row(isolated_db, loop.session) is not None


def test_streak_below_threshold_is_not_latched(loop, isolated_db):  # noqa: F811
    loop.engine.account_status.consecutive_losses = 2
    assert loop.tick() == []
    assert _latch_row(isolated_db, loop.session) is None


# ----------------------------------------------------------------------
# Attach refusals
# ----------------------------------------------------------------------
def test_attach_refuses_a_different_policy(loop, isolated_db):  # noqa: F811
    engine = loop.engine
    engine.policy = dict(engine.policy, BASE_RISK_PCT=0.02)
    original = engine._enforce_execution_feedback_integrity
    assert loop.session.attach(engine) is False
    assert engine._enforce_execution_feedback_integrity == original
    refused = risk_events(isolated_db, "LIVE_STARTUP_REFUSED")
    assert len(refused) == 1 and refused[0]["reason"].startswith("LIVE_POLICY_HASH_MISMATCH")
    assert live_gate.active_session() is None


def test_attach_refuses_a_different_strategy(loop, isolated_db):  # noqa: F811
    loop.engine.strategy_id = "other_strategy_v2"
    assert loop.session.attach(loop.engine) is False
    assert risk_events(isolated_db, "LIVE_STARTUP_REFUSED")[0]["reason"].startswith("LIVE_STRATEGY_MISMATCH")


def test_attach_refuses_a_mock_engine(loop, isolated_db):  # noqa: F811
    loop.engine.use_mock = True
    assert loop.session.attach(loop.engine) is False
    assert risk_events(isolated_db, "LIVE_STARTUP_REFUSED")[0]["reason"].startswith("LIVE_MOCK_FORBIDDEN")


# ----------------------------------------------------------------------
# Waiver reminders and internal errors
# ----------------------------------------------------------------------
def test_waiver_reminder_fires_every_four_hours(tmp_path, isolated_db, caplog):  # noqa: F811
    loop = Loop(make_scenario(tmp_path, waiver=True))
    assert loop.tick() == []
    assert health._health_status["live_evidence_waiver"] is True
    assert "no micro_live replay yet" in health._health_status["live_waiver_reason"]
    assert loop.engine.rules() == []
    loop.now = loop.scenario.now + timedelta(hours=3, minutes=59)
    loop.tick(0)
    assert loop.engine.rules() == []
    loop.now = loop.scenario.now + timedelta(hours=4)
    with caplog.at_level(logging.CRITICAL, logger="fx_ai_engine.live_gate"):
        loop.tick(0)
    assert loop.engine.rules() == ["LIVE_EVIDENCE_WAIVER"]
    assert loop.engine.events[0][1] == "CRITICAL"
    assert any(r.levelno == logging.CRITICAL and "WAIVER" in r.getMessage() for r in caplog.records)
    loop.tick(60)
    assert loop.engine.rules() == ["LIVE_EVIDENCE_WAIVER"]
    loop.now = loop.scenario.now + timedelta(hours=8, minutes=1)
    loop.tick(0)
    assert loop.engine.rules() == ["LIVE_EVIDENCE_WAIVER", "LIVE_EVIDENCE_WAIVER"]


def test_waiver_reminder_uses_the_injectable_clock(tmp_path, isolated_db):  # noqa: F811
    loop = Loop(make_scenario(tmp_path, waiver=True))
    later = loop.scenario.now + timedelta(hours=4, seconds=1)
    write_status(loop.scenario.bridge_dir, later)
    loop.engine.account_status.updated_at = later
    loop.session._clock = lambda: later
    loop.session.check_loop(loop.engine)
    assert "LIVE_EVIDENCE_WAIVER" in loop.engine.rules()


def test_no_waiver_reminder_without_a_waiver(loop):
    loop.tick(60 * 60 * 5, refresh_status=True)
    assert "LIVE_EVIDENCE_WAIVER" not in loop.engine.rules()


def test_internal_error_halts(loop, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(live_gate, "read_ea_status", boom)
    codes = loop.tick()
    _halted(loop, "LIVE_GATE_INTERNAL_ERROR", codes)
    assert ("LIVE_GATE_INTERNAL_ERROR", "CRITICAL") in [(e[0], e[1]) for e in loop.engine.events]


def test_internal_error_in_the_loss_check_halts(loop, monkeypatch):
    monkeypatch.setattr(loop.session, "_check_loss", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    _halted(loop, "LIVE_GATE_INTERNAL_ERROR", loop.tick())


def test_database_failure_reading_the_latch_halts(loop, monkeypatch):
    monkeypatch.setattr(
        live_gate.db, "get_engine_runtime_state", lambda *a, **k: (_ for _ in ()).throw(OSError("locked"))
    )
    _halted(loop, "LIVE_GATE_INTERNAL_ERROR", loop.tick())


def test_event_persistence_failure_does_not_lift_the_halt(loop):
    (loop.scenario.bridge_dir / "KILL_SWITCH").write_text("stop", encoding="utf-8")

    def broken(*_a, **_k):
        raise RuntimeError("db down")

    loop.engine._insert_risk_event = broken
    codes = loop.tick()
    _halted(loop, "LIVE_KILL_SWITCH", codes)


def test_several_conditions_report_together(loop):
    (loop.scenario.bridge_dir / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    loop.engine.account_status.equity = 1.0
    codes = loop.tick()
    assert {"LIVE_KILL_SWITCH", "LIVE_LOSS_LIMIT"} <= set(codes)
    assert health._health_status["live_halt_reasons"] == codes
