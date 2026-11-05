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
    OutageState,
    bridge_freshness,
    host_guard_disabled_reason,
)
from core import alerts, host_rules
from core.host_rules import BLOCK, PASS, WARN, Check
from tests.engine_harness import make_file_bridge_engine
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)
from tests.test_file_bridge_connection import build_bridge
from tests.test_host_check import NOW, _bridge, _fake_runner, _mt5_dir

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
    assert rec[0][2].startswith("status=PASS scope=guard failed=- swap_used_mb=1700 checks=power_source:PASS")
    guard.poll(T0.replace(hour=12), rec)
    guard.poll(T0.replace(hour=12) + timedelta(days=1), rec)
    assert rec.rules() == [HOST_GUARD_DAILY_CHECK, HOST_GUARD_DAILY_CHECK]


def test_daily_check_records_a_block_status():
    rec = Recorder()
    _guard(Probe(_checks(power=BLOCK)), window=lambda now: True).poll(T0, rec)
    assert rec.rules() == [HOST_GUARD_BLOCK, HOST_GUARD_DAILY_CHECK]
    assert rec[1][2].startswith("status=BLOCK scope=guard failed=power_source ")


def test_window_start_record_reports_a_memory_block_without_blocking_entries():
    guard_probe = Probe(_checks())
    window_probe = Probe([*_checks(), Check("memory_free_pct", BLOCK, "free=10%"), Check("disk_free", PASS, "free=120 GB")])
    rec = Recorder()
    guard = HostGuard(guard_probe, evaluate_window_start=window_probe, in_trading_window=lambda now: True)
    status = guard.poll(T0, rec)
    assert not status.blocked
    assert rec.rules() == [HOST_GUARD_DAILY_CHECK]
    assert rec[0][2].startswith("status=BLOCK scope=host_check failed=memory_free_pct swap_used_mb=500 ")
    assert "memory_free_pct:BLOCK" in rec[0][2]
    assert (window_probe.calls, guard_probe.calls) == (1, 0)
    guard.poll(T0 + timedelta(seconds=300), rec)  # periodic polls use the cheap guard rules
    assert (window_probe.calls, guard_probe.calls) == (1, 1)


def test_production_rule_sets_through_the_guard(tmp_path):
    """guard_checks for polling, window_start_checks at the window start, on a fake host."""
    bridge, mt5_dir = _bridge(tmp_path), _mt5_dir(tmp_path)
    run = _fake_runner(disk_gb=2)
    rec = Recorder()
    guard = HostGuard(
        lambda now: host_rules.guard_checks(run, bridge, now),
        evaluate_window_start=lambda now: host_rules.window_start_checks(run, bridge, now, mt5_dir),
        in_trading_window=lambda now: True,
    )
    assert not guard.poll(NOW, rec).blocked
    assert rec.rules() == [HOST_GUARD_DAILY_CHECK]
    assert rec[0][2].startswith("status=BLOCK scope=host_check failed=disk_free ")


def test_failed_replay_probe_blocks_entries(tmp_path):
    run, bridge = _fake_runner(failing_pgrep=("research.",)), _bridge(tmp_path)
    rec = Recorder()
    status = _guard(lambda now: host_rules.guard_checks(run, bridge, NOW)).poll(T0, rec)
    assert status.blocked and status.failed == ("no_replay_jobs",)
    assert "probe failed" in status.details
    assert rec.rules() == [HOST_GUARD_BLOCK]


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
    monitor = host_tolerance.build_outage_monitor(use_mock=False, bridge_path=tmp_path)
    assert isinstance(monitor, OutageMonitor)
    assert monitor.state.path == tmp_path / "outage_alert_state.json"  # conftest moves the default into tmp_path


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


def _monitor(ages, sender, *, state_path, configured=True, hours=lambda now: True):
    reads = SimpleNamespace(calls=0)

    def read(now):
        reads.calls += 1
        return dict(ages)

    monitor = OutageMonitor(read, state=OutageState(state_path), send=sender, is_configured=lambda: configured,
                            in_trading_hours=hours)
    return monitor, reads


def test_outage_sends_exactly_two_messages(tmp_path):
    ages = {"account_snapshot": 3.0, "bar_exports": 5.0}
    sender = Sender()
    monitor, _ = _monitor(ages, sender, state_path=tmp_path / "state.json")
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


def test_outage_outside_trading_hours_alerts_only_once_the_window_opens(tmp_path):
    ages = {"account_snapshot": 900.0, "bar_exports": 900.0}
    sender = Sender()
    in_hours = {"value": False}
    monitor, _ = _monitor(ages, sender, state_path=tmp_path / "state.json", hours=lambda now: in_hours["value"])
    assert monitor.poll(T0) is None and sender == []
    in_hours["value"] = True
    assert monitor.poll(T0 + timedelta(minutes=5)) == "OUTAGE"
    assert len(sender) == 1


def test_missing_feed_counts_as_stale(tmp_path):
    sender = Sender()
    monitor, _ = _monitor({"account_snapshot": None, "bar_exports": 1.0}, sender, state_path=tmp_path / "s.json")
    assert monitor.poll(T0) == "OUTAGE" and "account_snapshot missing" in sender[0]


def test_no_alert_channel_means_nothing_is_read_or_sent(tmp_path):
    sender = Sender()
    state_path = tmp_path / "state.json"
    monitor, reads = _monitor({"account_snapshot": None, "bar_exports": None}, sender, state_path=state_path,
                              configured=False)
    assert monitor.poll(T0) is None
    assert reads.calls == 0 and sender == []
    assert not state_path.exists() and not state_path.with_suffix(".lock").exists()


@pytest.mark.parametrize("result", [False, RuntimeError("network down")])
def test_failed_delivery_does_not_raise_or_repeat(result, tmp_path):
    sender = Sender(result)
    monitor, _ = _monitor({"account_snapshot": 500.0, "bar_exports": 1.0}, sender, state_path=tmp_path / "s.json")
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


# ---------------------------------------------------------------- outage episode across processes and restarts
def test_restart_during_an_outage_sends_no_second_outage_message(tmp_path):
    ages = {"account_snapshot": 300.0, "bar_exports": 5.0}
    sender, path = Sender(), tmp_path / "state.json"
    first, _ = _monitor(ages, sender, state_path=path)
    assert first.poll(T0) == "OUTAGE"
    restarted, _ = _monitor(ages, sender, state_path=path)  # engine restarted, outage continues
    assert restarted.poll(T0 + timedelta(minutes=5)) is None
    ages["account_snapshot"] = 2.0
    assert restarted.poll(T0 + timedelta(minutes=10)) == "RECOVERED"
    assert len(sender) == 2 and "account_snapshot fresh again" in sender[1]
    assert json.loads(path.read_text())["active"] is False


def test_recovery_while_the_engine_was_down_is_reported_once_on_restart(tmp_path):
    ages = {"account_snapshot": 300.0, "bar_exports": 5.0}
    sender, path = Sender(), tmp_path / "state.json"
    first, _ = _monitor(ages, sender, state_path=path)
    assert first.poll(T0) == "OUTAGE"
    ages["account_snapshot"] = 1.0
    restarted, _ = _monitor(ages, sender, state_path=path)
    assert restarted.poll(T0 + timedelta(hours=8)) == "RECOVERED"
    assert restarted.poll(T0 + timedelta(hours=8, seconds=30)) is None
    assert len(sender) == 2


def test_engine_and_watchdog_monitors_share_one_episode(tmp_path):
    ages = {"account_snapshot": 300.0, "bar_exports": 300.0}
    sender, path = Sender(), tmp_path / "state.json"
    engine_monitor, _ = _monitor(ages, sender, state_path=path)
    watchdog_monitor, _ = _monitor(ages, sender, state_path=path)
    events = [m.poll(T0 + timedelta(seconds=i)) for i, m in enumerate([engine_monitor, watchdog_monitor] * 3)]
    ages.update(account_snapshot=1.0, bar_exports=1.0)
    events += [m.poll(T0 + timedelta(minutes=5, seconds=i)) for i, m in enumerate([watchdog_monitor, engine_monitor] * 2)]
    assert [e for e in events if e] == ["OUTAGE", "RECOVERED"]
    assert len(sender) == 2


def test_unwritable_state_falls_back_to_memory_and_does_not_repeat(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("")
    sender = Sender()
    monitor, _ = _monitor({"account_snapshot": 300.0, "bar_exports": 1.0}, sender, state_path=blocker / "state.json")
    assert monitor.poll(T0) == "OUTAGE"
    assert monitor.poll(T0 + timedelta(seconds=30)) is None
    assert len(sender) == 1


def _configure_telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent = Sender()
    monkeypatch.setattr(alerts, "send_telegram_message", sent)  # the one Telegram call every alert goes through
    return sent


def test_snapshot_outage_sends_two_messages_through_the_production_paths(tmp_path, monkeypatch, isolated_db):
    """Engine risk recorder, demo run loop, an engine restart and the watchdog: one outage, two messages."""
    sent = _configure_telegram(monkeypatch)
    monkeypatch.setattr(host_tolerance, "is_tradeable_session", lambda now: True)
    now = datetime.now(timezone.utc)
    bridge_dir = build_bridge(tmp_path / "bridge", now, open_tickets=())
    engine = make_file_bridge_engine(bridge_dir, monkeypatch)
    run = _fake_runner()  # healthy host; only the bridge goes stale
    engine.host_guard = HostGuard(lambda t: host_rules.guard_checks(run, bridge_dir, t), in_trading_window=lambda t: False)

    snapshot_path = bridge_dir / "feedback" / "account_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["timestamp"] = (now - timedelta(seconds=150)).isoformat().replace("+00:00", "Z")
    snapshot_path.write_text(json.dumps(snapshot))  # the EA stopped writing 150 s ago

    status = engine._poll_host_guard(now)  # production recorder: Engine._insert_risk_event
    assert status.blocked and status.failed == ("bridge_snapshot_fresh",)
    engine.run(mode="demo", iterations=1)  # stops: the demo account cannot be verified on a stale snapshot
    assert sent == []
    assert (HOST_GUARD_BLOCK, "BLOCK") in _rows(isolated_db, "SELECT rule_name, severity FROM risk_events")

    assert engine.outage_monitor.poll(now) == "OUTAGE"
    watchdog_monitor = host_tolerance.build_outage_monitor(use_mock=False, bridge_path=bridge_dir)
    assert watchdog_monitor.poll(now + timedelta(seconds=30)) is None
    restarted_engine_monitor = host_tolerance.build_outage_monitor(use_mock=False, bridge_path=bridge_dir)
    assert restarted_engine_monitor.poll(now + timedelta(seconds=45)) is None

    snapshot["timestamp"] = (now + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    snapshot_path.write_text(json.dumps(snapshot))  # the EA writes again
    assert watchdog_monitor.poll(now + timedelta(seconds=60)) == "RECOVERED"
    assert restarted_engine_monitor.poll(now + timedelta(seconds=61)) is None
    assert not engine._poll_host_guard(now + timedelta(seconds=60)).blocked

    assert len(sent) == 2
    assert "BRIDGE OUTAGE" in sent[0] and "account_snapshot" in sent[0]
    assert "BRIDGE RECOVERED" in sent[1]
    rules = [r[0] for r in _rows(isolated_db, "SELECT rule_name FROM risk_events")]
    assert rules.count(HOST_GUARD_BLOCK) == 1 and rules.count(HOST_GUARD_RECOVERED) == 1


def test_other_engine_block_events_still_alert(tmp_path, monkeypatch, isolated_db):
    sent = _configure_telegram(monkeypatch)
    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    engine = make_file_bridge_engine(bridge_dir, monkeypatch)
    engine._insert_risk_event("HARD_RISK", "BLOCK", "daily stop reached")
    assert len(sent) == 1 and "HARD_RISK" in sent[0]


# ---------------------------------------------------------------- watchdog
def test_watchdog_builds_the_shared_monitor_and_survives_poll_errors(tmp_path, monkeypatch):
    import watchdog

    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    monkeypatch.setenv("BRIDGE_BASE_PATH", str(bridge_dir))
    monkeypatch.delenv("USE_MT5_MOCK", raising=False)
    monitor = watchdog._build_outage_monitor()
    assert isinstance(monitor, OutageMonitor)
    assert monitor.state.path == host_tolerance.DEFAULT_OUTAGE_STATE_PATH

    monkeypatch.setenv("USE_MT5_MOCK", "1")
    assert watchdog._build_outage_monitor() is None

    def broken(now):
        raise RuntimeError("disk gone")

    watchdog._poll_outage(SimpleNamespace(poll=broken))  # logged, not raised
    watchdog._poll_outage(None)
