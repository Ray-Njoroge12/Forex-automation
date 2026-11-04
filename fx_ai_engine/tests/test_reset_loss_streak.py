"""The explicit operator command that clears the live loss-streak halt."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from config_microcapital import get_policy_config
from core import live_gate
from core.account_status import AccountStatus
from core.evidence import build_runtime_evidence_context
from database import db
from tests.live_fixtures import LOGIN, SERVER, make_scenario, risk_events
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)
from tests.test_live_gate_loop import Loop
from validation import reset_loss_streak

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
CONFIRM = f"RESET-LOSS-STREAK-{LOGIN}"


def _context(login=LOGIN, server=SERVER):
    return build_runtime_evidence_context(
        get_policy_config("micro_live", env={}), use_mock=False, login=login, server=server
    )


def _argv(**overrides):
    values = {
        "--account-login": str(LOGIN),
        "--account-server": SERVER,
        "--operator": "Owner",
        "--reason": "reviewed the three losses",
        "--confirm-text": CONFIRM,
    }
    values.update(overrides)
    argv = []
    for key, value in values.items():
        if value is not None:
            argv += [key, value]
    return argv


def _dead_probe():
    return None


def _run(argv=None, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("health_probe", _dead_probe)
    return reset_loss_streak.main(argv if argv is not None else _argv(), **kwargs)


def _seed(streak=3, age=timedelta(hours=1), *, context=None, latch=True, base=NOW):
    context = context or _context()
    status = AccountStatus(
        balance=9.0, equity=8.5, consecutive_losses=streak, is_trading_halted=True, updated_at=base - age
    )
    db.insert_account_metrics(status, evidence_context=context)
    if latch:
        db.set_engine_runtime_state(
            live_gate.LOSS_LATCH_KEY, "latched_at=2026-11-05T10:00:00+00:00 consecutive_losses=3", evidence_context=context
        )
    return context


def _latch(context=None):
    return db.get_engine_runtime_state(live_gate.LOSS_LATCH_KEY, evidence_context=context or _context())


def _latest(context=None):
    context = context or _context()
    return db.get_latest_account_metric(evidence_stream=context.evidence_stream, account_scope=context.account_scope)


@pytest.fixture(autouse=True)
def _clean_session():
    live_gate.reset_active_session()
    yield
    live_gate.reset_active_session()


def test_help_exits_zero(capsys):
    assert reset_loss_streak.main(["--help"]) == 0


@pytest.mark.parametrize("text", ["", "reset", "reset-loss-streak-7001234567", CONFIRM + " ", "RESET-LOSS-STREAK-1"])
def test_wrong_confirmation_refuses(isolated_db, text):  # noqa: F811
    _seed()
    assert _run(_argv(**{"--confirm-text": text})) == 2
    assert _latch() is not None
    assert risk_events(isolated_db, "LIVE_LOSS_STREAK_RESET") == []


def test_prompted_confirmation_is_used(isolated_db):  # noqa: F811
    _seed()
    prompts = []
    assert _run(_argv(**{"--confirm-text": None}), input_fn=lambda p: prompts.append(p) or "nope") == 2
    assert CONFIRM in prompts[0]
    assert _latch() is not None
    assert _run(_argv(**{"--confirm-text": None}), input_fn=lambda p: CONFIRM) == 0
    assert _latch() is None


def test_empty_prompt_and_eof_refuse(isolated_db):  # noqa: F811
    _seed()
    assert _run(_argv(**{"--confirm-text": None}), input_fn=lambda p: "") == 2

    def eof(prompt):
        raise EOFError

    assert _run(_argv(**{"--confirm-text": None}), input_fn=eof) == 2
    assert _latch() is not None


@pytest.mark.parametrize("missing", ["--operator", "--reason"])
def test_missing_operator_or_reason_refuses(isolated_db, missing):  # noqa: F811
    _seed()
    assert _run(_argv(**{missing: None})) == 2
    assert _run(_argv(**{missing: "   "})) == 2
    assert _latch() is not None
    assert risk_events(isolated_db, "LIVE_LOSS_STREAK_RESET") == []


def test_recent_metrics_row_refuses(isolated_db):  # noqa: F811
    _seed(age=timedelta(seconds=30))
    assert _run() == 2
    assert _latch() is not None
    assert risk_events(isolated_db, "LIVE_LOSS_STREAK_RESET") == []


def test_probe_alive_refuses(isolated_db):  # noqa: F811
    _seed()
    beat = json.dumps({"last_heartbeat": (NOW - timedelta(seconds=10)).isoformat()})
    assert _run(health_probe=lambda: beat) == 2
    assert _latch() is not None


def test_probe_garbage_refuses(isolated_db):  # noqa: F811
    _seed()
    for body in ("<html>hello</html>", b"\xff\xfe", "{}", json.dumps({"last_heartbeat": None}), json.dumps({"last_heartbeat": "yesterday"})):
        assert _run(health_probe=lambda body=body: body) == 2
    assert _latch() is not None


def test_probe_raising_refuses(isolated_db):  # noqa: F811
    _seed()

    def broken():
        raise OSError("network unreachable")

    assert _run(health_probe=broken) == 2
    assert _latch() is not None


def test_database_read_failure_refuses(isolated_db, monkeypatch):  # noqa: F811
    _seed()

    def boom(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "get_latest_account_metric", boom)
    assert _run() == 2
    assert _latch() is not None


def test_success_with_an_old_row(isolated_db):  # noqa: F811
    _seed()
    old_beat = json.dumps({"last_heartbeat": (NOW - timedelta(hours=2)).isoformat()})
    assert _run(health_probe=lambda: old_beat) == 0
    latest = _latest()
    assert latest["consecutive_losses"] == 0
    assert latest["is_trading_halted"] == 0
    assert latest["equity"] == 8.5
    assert _latch() is None
    events = risk_events(isolated_db, "LIVE_LOSS_STREAK_RESET")
    assert len(events) == 1
    assert events[0]["severity"] == "INFO"
    assert "Owner" in events[0]["reason"]
    assert "reviewed the three losses" in events[0]["reason"]
    assert "previous_streak=3" in events[0]["reason"]
    assert events[0]["account_scope"] == _context().account_scope


def test_latch_without_any_metrics_row_is_still_cleared(isolated_db):  # noqa: F811
    db.set_engine_runtime_state(live_gate.LOSS_LATCH_KEY, "latched", evidence_context=_context())
    assert _run() == 0
    assert _latch() is None
    assert len(risk_events(isolated_db, "LIVE_LOSS_STREAK_RESET")) == 1


def test_gate_stops_halting_on_the_latch_after_the_reset(tmp_path, isolated_db):  # noqa: F811
    loop = Loop(make_scenario(tmp_path))
    assert loop.session.evidence_context.account_scope == _context().account_scope
    loop.engine.account_status.consecutive_losses = 3
    assert "LIVE_LOSS_STREAK_LATCHED" in loop.tick()
    assert _latch() is not None
    # An engine that stopped hours ago: its last metrics row is old.
    _seed(streak=3, age=timedelta(hours=3), latch=False, base=datetime.now(timezone.utc))
    assert _run(now=datetime.now(timezone.utc)) == 0
    assert _latch() is None
    restarted = Loop(loop.scenario)
    assert restarted.engine.account_status.consecutive_losses == 0
    assert restarted.tick() == []
    assert restarted.engine.account_status.is_trading_halted is False


def test_other_account_scope_is_untouched(isolated_db):  # noqa: F811
    other = _context(login=LOGIN + 1)
    _seed(context=other)
    _seed()
    before = _latest(other)
    assert _run() == 0
    assert _latch(other) is not None
    assert _latest(other) == before
    assert risk_events(isolated_db, "LIVE_LOSS_STREAK_RESET")[0]["account_scope"] != other.account_scope


def test_nothing_to_reset_exits_zero_without_writing(isolated_db, capsys):  # noqa: F811
    _seed(streak=0, latch=False)
    assert _run() == 0
    assert "nothing to reset" in capsys.readouterr().out
    assert risk_events(isolated_db) == []
    assert _latest()["timestamp"] == (NOW - timedelta(hours=1)).isoformat()


def test_nothing_to_reset_with_empty_database(isolated_db, capsys):  # noqa: F811
    assert _run() == 0
    assert "nothing to reset" in capsys.readouterr().out
    assert risk_events(isolated_db) == []


def test_failure_after_the_event_leaves_the_latch_set(isolated_db, monkeypatch):  # noqa: F811
    _seed()

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "insert_account_metrics", boom)
    assert _run() == 2
    assert len(risk_events(isolated_db, "LIVE_LOSS_STREAK_RESET")) == 1
    assert _latch() is not None


def test_failure_deleting_the_latch_exits_two(isolated_db, monkeypatch):  # noqa: F811
    _seed()

    def boom(context):
        raise RuntimeError("locked")

    monkeypatch.setattr(reset_loss_streak, "_delete_latch", boom)
    assert _run() == 2
    assert _latch() is not None
