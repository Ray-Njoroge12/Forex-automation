"""Engine host guard and outage alert (operating-model doc, section 6 items 1 and 3).

Every probe is injected: no test touches pmset, pgrep, defaults or Telegram.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from application import decision_cycle, host_tolerance
from application.host_tolerance import (
    HOST_GUARD_BLOCK,
    HOST_GUARD_DAILY_CHECK,
    HOST_GUARD_RECOVERED,
    HostGuard,
    OutageMonitor,
    bridge_freshness,
    host_guard_disabled_reason,
)
from core import alerts, host_rules
from core.host_rules import BLOCK, PASS, WARN, Check
from tests.engine_harness import make_file_bridge_engine
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)
from tests.test_file_bridge_connection import build_bridge
from tests.test_host_check import NOW, _bridge, _fake_runner

T0 = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)  # Wednesday, London session


def _checks(*, power=PASS, swap=PASS, swap_mb=500):
    return [
        Check("power_source", power, "power=AC" if power == PASS else "power=BATTERY"),
        Check("sleep_prevented", PASS, "caffeinate_running=True"),
        Check("mt5_app_nap_disabled", PASS, "NSAppSleepDisabled=1"),
        Check("no_replay_jobs", PASS, "replay/research processes=0"),
        Check("bridge_snapshot_fresh", PASS, "age=3s"),
        Check("swap_used", swap, f"swap_used={swap_mb} MB"),
    ]


class Probe:
    def __init__(self, checks):
        self.checks = checks
        self.calls = 0

    def __call__(self, now):
        self.calls += 1
        if isinstance(self.checks, Exception):
            raise self.checks
        return list(self.checks)


class Recorder(list):
    def __call__(self, rule, severity, reason):
        self.append((rule, severity, reason))

    def rules(self):
        return [r[0] for r in self]


def _guard(probe, *, window=lambda now: False):
    return HostGuard(probe, interval_seconds=300, blocked_interval_seconds=30, in_trading_window=window)


# ---------------------------------------------------------------- shared rules
def test_guard_checks_are_the_trading_rules_without_cpu_or_footprint_sampling(tmp_path):
    seen: list[str] = []
    runner = _fake_runner()

    def run(cmd):
        seen.append(cmd[0])
        return runner(cmd)

    checks = host_rules.guard_checks(run, _bridge(tmp_path), NOW)
    assert [c.check_id for c in checks] == [*host_rules.GUARD_BLOCKING_CHECKS[:3], "no_replay_jobs",
                                            "bridge_snapshot_fresh", "swap_used"]
    assert host_rules.blocking_failures(checks) == []
    assert "ps" not in seen and "footprint" not in seen and "memory_pressure" not in seen
    assert len(seen) <= 10


def test_guard_checks_block_on_each_backlog_condition(tmp_path):
    run = _fake_runner(power="BATTERY", awake=0, nap="", replay=True)
    failed = {c.check_id for c in host_rules.blocking_failures(host_rules.guard_checks(run, _bridge(tmp_path, age_s=600), NOW))}
    assert failed == set(host_rules.GUARD_BLOCKING_CHECKS)


def test_swap_alone_never_blocks_entries():
    assert host_rules.blocking_failures(_checks(swap=BLOCK, swap_mb=4500)) == []


@pytest.mark.parametrize("kwargs,expected", [
    ({"use_mock": True, "run_mode": "demo", "platform": "darwin"}, "mock mode"),
    ({"use_mock": False, "run_mode": "smoke", "platform": "darwin"}, "run mode smoke"),
    ({"use_mock": False, "run_mode": "demo", "platform": "linux"}, "host rules are macOS-specific (platform=linux)"),
    ({"use_mock": False, "run_mode": "demo", "platform": "darwin"}, ""),
    ({"use_mock": False, "run_mode": "live", "platform": "darwin"}, ""),
])
def test_guard_applies_only_to_real_demo_or_live_runs_on_macos(kwargs, expected):
    assert host_guard_disabled_reason(**kwargs) == expected


# ---------------------------------------------------------------- guard state machine
def test_pass_at_start_records_nothing_outside_the_window_and_respects_the_interval():
    probe, rec = Probe(_checks()), Recorder()
    guard = _guard(probe)
    assert not guard.poll(T0, rec).blocked
    guard.poll(T0 + timedelta(seconds=299), rec)
    assert probe.calls == 1
    guard.poll(T0 + timedelta(seconds=300), rec)
    assert probe.calls == 2
    assert rec == []


def test_block_is_recorded_once_and_recovery_once():
    probe, rec = Probe(_checks(power=BLOCK)), Recorder()
    guard = _guard(probe)
    status = guard.poll(T0, rec)
    assert status.blocked and status.failed == ("power_source",)
    assert rec.rules() == [HOST_GUARD_BLOCK] and rec[0][1] == "BLOCK"
    assert "power_source=BLOCK power=BATTERY" in rec[0][2]

    guard.poll(T0 + timedelta(seconds=10), rec)  # not due while blocked (30 s)
    guard.poll(T0 + timedelta(seconds=30), rec)  # due, same failure: no new event
    assert probe.calls == 2 and rec.rules() == [HOST_GUARD_BLOCK]

    probe.checks = _checks()
    status = guard.poll(T0 + timedelta(seconds=60), rec)
    assert not status.blocked
    assert rec.rules() == [HOST_GUARD_BLOCK, HOST_GUARD_RECOVERED] and rec[1][1] == "INFO"


def test_a_changed_set_of_failures_is_recorded_again():
    probe, rec = Probe(_checks(power=BLOCK)), Recorder()
    guard = _guard(probe)
    guard.poll(T0, rec)
    probe.checks = [*_checks(power=BLOCK)[:3], Check("no_replay_jobs", BLOCK, "replay/research processes=2"), *_checks()[4:]]
    guard.poll(T0 + timedelta(seconds=30), rec)
    assert rec.rules() == [HOST_GUARD_BLOCK, HOST_GUARD_BLOCK]
    assert guard.status.failed == ("power_source", "no_replay_jobs")


def test_probe_failure_fails_closed():
    rec = Recorder()
    guard = _guard(Probe(RuntimeError("pmset missing")))
    status = guard.poll(T0, rec)
    assert status.blocked and status.failed == (host_tolerance.HOST_GUARD_PROBE_ERROR,)
    assert rec.rules() == [HOST_GUARD_BLOCK]


def test_daily_check_is_recorded_once_per_utc_day_at_the_first_poll_in_the_window():
    probe, rec = Probe(_checks(swap=WARN, swap_mb=1700)), Recorder()
    window = lambda now: 7 <= now.hour < 21  # noqa: E731
    guard = _guard(probe, window=window)
    guard.poll(T0.replace(hour=6, minute=55), rec)  # before the window: evaluated, not recorded
    assert rec == []
    guard.poll(T0.replace(hour=7, minute=0), rec)  # window opens: forced evaluation inside the 300 s interval
    assert probe.calls == 2
    assert rec.rules() == [HOST_GUARD_DAILY_CHECK]
    assert rec[0][1] == "INFO"
    assert rec[0][2].startswith("status=PASS failed=- swap_used_mb=1700 checks=power_source:PASS")
    guard.poll(T0.replace(hour=12), rec)
    guard.poll(T0.replace(hour=12) + timedelta(days=1), rec)
    assert rec.rules() == [HOST_GUARD_DAILY_CHECK, HOST_GUARD_DAILY_CHECK]


def test_daily_check_records_a_block_status():
    rec = Recorder()
    _guard(Probe(_checks(power=BLOCK)), window=lambda now: True).poll(T0, rec)
    assert rec.rules() == [HOST_GUARD_BLOCK, HOST_GUARD_DAILY_CHECK]
    assert rec[1][2].startswith("status=BLOCK failed=power_source ")


def test_disabled_guard_never_probes():
    rec = Recorder()
    guard = HostGuard.disabled("mock mode")
    status = guard.poll(T0, rec)
    assert not status.enabled and not status.blocked and rec == []


# ---------------------------------------------------------------- engine wiring
def _engine(tmp_path, monkeypatch, guard):
    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    engine = make_file_bridge_engine(bridge_dir, monkeypatch)
    engine.host_guard = guard
    monkeypatch.setattr(decision_cycle, "evaluate_calendar_freshness", lambda *a, **k: SimpleNamespace(approved=True))
    monkeypatch.setattr(decision_cycle, "is_tradeable_session", lambda now: True)
    evaluated: list[str] = []
    engine._evaluate_symbol = lambda sym, *_a: evaluated.append(sym)
    return engine, bridge_dir, evaluated


def _rows(connection, sql):
    with connection() as conn:
        return [tuple(r) for r in conn.execute(sql)]


def test_blocked_host_refuses_new_entries_with_reason_codes(tmp_path, monkeypatch, isolated_db):
    probe = Probe(_checks(power=BLOCK))
    engine, bridge_dir, evaluated = _engine(tmp_path, monkeypatch, _guard(probe))

    engine._decision_cycle()

    assert evaluated == []
    funnel = _rows(isolated_db, "SELECT stage, outcome, reason_code FROM decision_funnel_events")
    assert funnel == [("HOST", "REJECT", HOST_GUARD_BLOCK)] * 6
    assert _rows(isolated_db, "SELECT rule_name, severity FROM risk_events") == [(HOST_GUARD_BLOCK, "BLOCK")]
    assert list((bridge_dir / "pending_signals").iterdir()) == []

    probe.checks = _checks()
    engine.host_guard._last_evaluated -= timedelta(seconds=30)  # the blocked re-check interval has passed
    engine._decision_cycle()
    assert len(evaluated) == 6
    assert (HOST_GUARD_RECOVERED, "INFO") in _rows(isolated_db, "SELECT rule_name, severity FROM risk_events")


def test_run_loop_checks_the_host_at_start(tmp_path, monkeypatch, isolated_db):
    probe = Probe(_checks(power=BLOCK))
    engine, _bridge_dir, _evaluated = _engine(tmp_path, monkeypatch, _guard(probe))
    engine._decision_cycle = lambda: None
    engine.run(mode="demo", iterations=1)
    assert probe.calls == 1
    assert engine.host_guard.status.blocked
    assert (HOST_GUARD_BLOCK, "BLOCK") in _rows(isolated_db, "SELECT rule_name, severity FROM risk_events")


def test_mock_mode_has_no_outage_monitor(tmp_path):
    assert host_tolerance.build_outage_monitor(use_mock=True, bridge_path=tmp_path) is None
    assert isinstance(host_tolerance.build_outage_monitor(use_mock=False, bridge_path=tmp_path), OutageMonitor)


# ---------------------------------------------------------------- outage alert
class Sender(list):
    def __init__(self, result=True):
        super().__init__()
        self.result = result

    def __call__(self, text):
        self.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _monitor(ages, sender, *, configured=True, hours=lambda now: True):
    reads = SimpleNamespace(calls=0)

    def read(now):
        reads.calls += 1
        return dict(ages)

    monitor = OutageMonitor(read, send=sender, is_configured=lambda: configured, in_trading_hours=hours)
    return monitor, reads


def test_outage_sends_exactly_two_messages():
    ages = {"account_snapshot": 3.0, "bar_exports": 5.0}
    sender = Sender()
    monitor, _ = _monitor(ages, sender)
    assert monitor.poll(T0) is None
    ages["account_snapshot"] = 119.0
    assert monitor.poll(T0) is None  # not yet more than 2 minutes
    ages["account_snapshot"] = 121.0
    assert monitor.poll(T0) == "OUTAGE"
    for minutes in range(1, 60):
        ages["account_snapshot"] = 121.0 + 60 * minutes
        ages["bar_exports"] = None if minutes > 30 else 5.0
        assert monitor.poll(T0 + timedelta(minutes=minutes)) is None
    ages.update(account_snapshot=2.0, bar_exports=4.0)
    assert monitor.poll(T0 + timedelta(hours=1)) == "RECOVERED"
    assert monitor.poll(T0 + timedelta(hours=1, seconds=5)) is None
    assert len(sender) == 2
    assert "BRIDGE OUTAGE" in sender[0] and "account_snapshot 121s old" in sender[0]
    assert "BRIDGE RECOVERED" in sender[1]


def test_outage_outside_trading_hours_alerts_only_once_the_window_opens():
    ages = {"account_snapshot": 900.0, "bar_exports": 900.0}
    sender = Sender()
    in_hours = {"value": False}
    monitor, _ = _monitor(ages, sender, hours=lambda now: in_hours["value"])
    assert monitor.poll(T0) is None and sender == []
    in_hours["value"] = True
    assert monitor.poll(T0 + timedelta(minutes=5)) == "OUTAGE"
    assert len(sender) == 1


def test_missing_feed_counts_as_stale():
    sender = Sender()
    monitor, _ = _monitor({"account_snapshot": None, "bar_exports": 1.0}, sender)
    assert monitor.poll(T0) == "OUTAGE" and "account_snapshot missing" in sender[0]


def test_no_alert_channel_means_nothing_is_read_or_sent():
    sender = Sender()
    monitor, reads = _monitor({"account_snapshot": None, "bar_exports": None}, sender, configured=False)
    assert monitor.poll(T0) is None
    assert reads.calls == 0 and sender == []


@pytest.mark.parametrize("result", [False, RuntimeError("network down")])
def test_failed_delivery_does_not_raise_or_repeat(result):
    sender = Sender(result)
    monitor, _ = _monitor({"account_snapshot": 500.0, "bar_exports": 1.0}, sender)
    assert monitor.poll(T0) == "OUTAGE"
    assert monitor.poll(T0 + timedelta(seconds=5)) is None
    assert len(sender) == 1


def test_alerts_configured_needs_flag_token_and_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert not alerts.alerts_configured()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    assert alerts.alerts_configured()
    monkeypatch.setenv("TELEGRAM_ENABLED", "0")
    assert not alerts.alerts_configured()


def test_bridge_freshness_reads_snapshot_status_and_bar_lag(tmp_path):
    now = datetime.now(timezone.utc)
    bridge_dir = build_bridge(tmp_path / "bridge", now, open_tickets=())
    ages = bridge_freshness(bridge_dir, now)
    assert 0 <= ages["account_snapshot"] < 10
    assert 0 <= ages["bar_exports"] < 10

    status_path = bridge_dir / "market" / "status.json"
    status = json.loads(status_path.read_text())
    for per_symbol in status["latest_closed_bars"].values():
        per_symbol["M15"] -= 3600  # bars stopped an hour ago while status.json kept updating
    status_path.write_text(json.dumps(status))
    assert bridge_freshness(bridge_dir, now)["bar_exports"] > 120

    (bridge_dir / "feedback" / "account_snapshot.json").unlink()
    assert bridge_freshness(bridge_dir, now)["account_snapshot"] is None


def test_engine_polls_the_outage_monitor_in_the_run_loop(tmp_path, monkeypatch, isolated_db):
    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    engine = make_file_bridge_engine(bridge_dir, monkeypatch)
    assert isinstance(engine.outage_monitor, OutageMonitor)  # built for the real bridge
    polls: list[datetime] = []
    engine.outage_monitor = SimpleNamespace(poll=lambda now: polls.append(now))
    engine._decision_cycle = lambda: None
    engine.run(mode="demo", iterations=1)
    assert len(polls) == 1
