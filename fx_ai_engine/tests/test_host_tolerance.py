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
from core import alerts, host_rules, live_gate
from core.file_bridge_connection import FileBridgeConnection
from core.host_rules import BLOCK, PASS, WARN, Check
from core.metrics import init_metrics
from core.observability import init_tracing
from tests.engine_harness import make_file_bridge_engine
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)
from tests.test_file_bridge_connection import build_bridge
from tests.test_host_check import NOW, _bridge, _fake_runner, _mt5_dir
from tests.test_live_mode_main import _live_scenario, runtime  # noqa: F401  (runtime is a fixture)

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


def test_signal_killed_pgrep_blocks_entries(tmp_path, monkeypatch):
    """HT-008: pgrep through the real run_command, killed by SIGKILL (return code -9)."""
    monkeypatch.setattr(host_rules.subprocess, "run", lambda cmd, **_k: SimpleNamespace(returncode=-9, stdout=""))
    fake, bridge = _fake_runner(awake=1), _bridge(tmp_path)

    def run(cmd):
        return host_rules.run_command(cmd) if cmd[0] == "pgrep" else fake(cmd)

    rec = Recorder()
    status = _guard(lambda now: host_rules.guard_checks(run, bridge, NOW)).poll(T0, rec)
    assert status.blocked and "no_replay_jobs" in status.failed
    assert "killed by signal 9" in status.details
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


def _age_bar_file(bridge_dir, symbol, seconds):
    """Shift the last bar of one M15 export back: that export stopped *seconds* ago."""
    path = bridge_dir / "market" / f"bars_{symbol}_M15.json"
    payload = json.loads(path.read_text())
    for row in payload["bars"]:
        row[0] -= seconds
    path.write_text(json.dumps(payload))


def _advance_status(bridge_dir, now):
    """The EA heartbeat keeps running: status.json is fresh and reports the current iTime bars."""
    path = bridge_dir / "market" / "status.json"
    status = json.loads(path.read_text())
    status["generated_at"] = now.isoformat().replace("+00:00", "Z")
    current_open = int(now.timestamp()) // 900 * 900
    for per_symbol in status["latest_closed_bars"].values():
        per_symbol["M15"] = current_open - 900
    path.write_text(json.dumps(status))


def test_bar_export_symbols_are_the_engine_symbols():
    from application.market_data_service import SYMBOLS

    assert host_tolerance.BAR_EXPORT_SYMBOLS == tuple(SYMBOLS)


def test_bridge_freshness_reads_the_snapshot_the_status_and_every_bar_file(tmp_path):
    now = datetime.now(timezone.utc)
    bridge_dir = build_bridge(tmp_path / "bridge", now, open_tickets=())
    ages = bridge_freshness(bridge_dir, now)
    assert 0 <= ages["account_snapshot"] < 10
    assert 0 <= ages["bar_exports"] < 10

    _advance_status(bridge_dir, now)
    _age_bar_file(bridge_dir, "USDCHF", 3600)  # one export stopped an hour ago; status.json is current
    assert bridge_freshness(bridge_dir, now)["bar_exports"] > 3000

    (bridge_dir / "market" / "bars_USDCHF_M15.json").unlink()
    assert bridge_freshness(bridge_dir, now)["bar_exports"] is None

    (bridge_dir / "feedback" / "account_snapshot.json").unlink()
    assert bridge_freshness(bridge_dir, now)["account_snapshot"] is None


def test_bar_files_are_parsed_again_only_when_they_change(tmp_path, monkeypatch):
    import os

    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    path = bridge_dir / "market" / "bars_EURUSD_M15.json"
    old = datetime.now(timezone.utc).timestamp() - 60
    os.utime(path, (old, old))
    reads: list[str] = []
    real_read = type(path).read_text
    monkeypatch.setattr(type(path), "read_text", lambda self, *a, **k: (reads.append(self.name), real_read(self, *a, **k))[1])
    first = host_tolerance.last_m15_bar_open(path)
    assert host_tolerance.last_m15_bar_open(path) == first and reads == [path.name]
    _age_bar_file(bridge_dir, "EURUSD", 900)  # rewritten now: parsed again, and not cached while racy
    assert host_tolerance.last_m15_bar_open(path) == first - 900
    assert host_tolerance.last_m15_bar_open(path) == first - 900
    assert reads.count(path.name) >= 3


@pytest.mark.parametrize("failure", ["stale", "missing"])
def test_a_failed_bar_export_alerts_while_status_stays_fresh_and_advances(failure, tmp_path, monkeypatch):
    """HT-005: status.json reports current iTime bars, but one bar file is not written."""
    sent = _configure_telegram(monkeypatch)
    monkeypatch.setattr(host_tolerance, "is_tradeable_session", lambda now: True)
    now = datetime.now(timezone.utc)
    bridge_dir = build_bridge(tmp_path / "bridge", now, open_tickets=())
    monitor = host_tolerance.build_outage_monitor(use_mock=False, bridge_path=bridge_dir)
    bar_file = bridge_dir / "market" / "bars_GBPUSD_M15.json"
    good = bar_file.read_text()

    if failure == "stale":
        _age_bar_file(bridge_dir, "GBPUSD", 1800)  # two bars missed
    else:
        bar_file.unlink()
    for step in range(3):
        _advance_status(bridge_dir, now + timedelta(seconds=20 * step))
        event = monitor.poll(now + timedelta(seconds=20 * step))
        assert event == ("OUTAGE" if step == 0 else None)
    assert "bar_exports" in sent[0] and "account_snapshot" not in sent[0]

    bar_file.write_text(good)  # the export is written again
    _advance_status(bridge_dir, now + timedelta(seconds=60))
    assert monitor.poll(now + timedelta(seconds=60)) == "RECOVERED"
    assert len(sent) == 2


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


def test_other_engine_block_events_still_alert(tmp_path, monkeypatch, isolated_db):
    sent = _configure_telegram(monkeypatch)
    monkeypatch.setattr(host_tolerance, "is_tradeable_session", lambda now: True)
    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    _age_snapshot(bridge_dir, 150)  # even during a snapshot outage
    engine = make_file_bridge_engine(bridge_dir, monkeypatch)
    engine._insert_risk_event("HARD_RISK", "BLOCK", "daily stop reached")
    assert len(sent) == 1 and "HARD_RISK" in sent[0]


def test_reports_covers_only_stale_feeds_the_outage_alert_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    in_hours = {"value": True}
    monkeypatch.setattr(host_tolerance, "is_tradeable_session", lambda now: in_hours["value"])
    now = datetime.now(timezone.utc)
    bridge_dir = build_bridge(tmp_path / "bridge", now, open_tickets=())
    monitor = host_tolerance.build_outage_monitor(use_mock=False, bridge_path=bridge_dir)

    assert not monitor.reports("STATE_STALE", now)  # snapshot fresh: not a bridge outage
    _age_snapshot(bridge_dir, 45)
    covered = [r for r in host_tolerance.OUTAGE_COVERED_RULES if monitor.reports(r, now)]
    assert covered == ["STATE_RECONCILIATION_FAILED", "STATE_STALE", "LIVE_EQUITY_STALE"]  # status.json is fresh
    assert not monitor.reports("HARD_RISK", now)
    in_hours["value"] = False  # outside trading hours the outage alert stays silent, so the event alerts
    assert not monitor.reports("STATE_STALE", now)
    monitor.state.save({"active": True})  # ... unless an episode that started in hours is still open
    assert monitor.reports("STATE_STALE", now)
    monkeypatch.setenv("TELEGRAM_ENABLED", "0")
    assert not monitor.reports("STATE_STALE", now)


# ---------------------------------------------------------------- production paths: main, run loop, watchdog
def _age_snapshot(bridge_dir, seconds):
    """The EA last wrote the account snapshot *seconds* ago (relative to the real clock)."""
    path = bridge_dir / "feedback" / "account_snapshot.json"
    snapshot = json.loads(path.read_text())
    snapshot["timestamp"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(snapshot))


@pytest.fixture
def demo_runtime(tmp_path, runtime, isolated_db):  # noqa: F811
    """main.main() in demo mode on a temp file bridge, with a healthy fake host behind the guard."""
    import main

    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    runtime.setenv("BRIDGE_BASE_PATH", str(bridge_dir))
    run = _fake_runner()  # power, sleep, App Nap and process probes pass; only the bridge can go stale
    guards: list[HostGuard] = []

    def build_guard(**kwargs):
        guard = HostGuard(lambda t: host_rules.guard_checks(run, kwargs["bridge_path"], t),
                          in_trading_window=lambda t: False)
        guards.append(guard)
        return guard

    runtime.setattr(host_tolerance, "build_host_guard", build_guard)
    created: list = []
    real_engine = main.Engine

    def recording_engine(*args, **kwargs):
        created.append(real_engine(*args, **kwargs))
        return created[-1]

    runtime.setattr(main, "Engine", recording_engine)
    return SimpleNamespace(
        bridge_dir=bridge_dir,
        engines=created,
        guards=guards,
        start=lambda: main.main(["--mode", "demo", "--iterations", "1"]),
    )


def _guard_events(isolated_db):
    return _rows(isolated_db, "SELECT rule_name, evidence_stream, account_scope FROM risk_events "
                              "WHERE rule_name LIKE 'HOST_GUARD_%' ORDER BY id")


def test_refused_demo_start_records_the_guard_block_and_a_later_start_the_recovery(demo_runtime, isolated_db):
    """HT-004, startup: no fresh demo snapshot means no engine, but the BLOCK is still persisted."""
    _age_snapshot(demo_runtime.bridge_dir, 45)
    assert demo_runtime.start() == 2
    assert demo_runtime.engines == []
    assert demo_runtime.start() == 2  # the watchdog retries during the outage: no second BLOCK
    _age_snapshot(demo_runtime.bridge_dir, 0)
    assert demo_runtime.start() == 0

    events = _guard_events(isolated_db)
    assert [e[0] for e in events] == [HOST_GUARD_BLOCK, HOST_GUARD_RECOVERED]
    engine_context = demo_runtime.engines[0].evidence_context
    assert {e[1:] for e in events} == {(engine_context.evidence_stream, engine_context.account_scope)}
    reason = _rows(isolated_db, f"SELECT reason FROM risk_events WHERE rule_name='{HOST_GUARD_BLOCK}'")[0][0]
    assert "bridge_snapshot_fresh=BLOCK" in reason


def test_running_demo_engine_records_the_block_when_the_snapshot_goes_stale(demo_runtime, isolated_db):
    """HT-004/HT-007, running loop: an unverified demo account records the host guard BLOCK."""
    assert demo_runtime.start() == 0
    engine = demo_runtime.engines[0]
    _age_snapshot(demo_runtime.bridge_dir, 45)
    engine.run(mode="demo", iterations=1)  # entries blocked; feedback and exits still processed
    assert engine.account_status.is_trading_halted
    assert [e[0] for e in _guard_events(isolated_db)] == [HOST_GUARD_BLOCK]

    _age_snapshot(demo_runtime.bridge_dir, 0)
    assert demo_runtime.start() == 0  # restarted process: the persisted BLOCK is recovered
    assert [e[0] for e in _guard_events(isolated_db)] == [HOST_GUARD_BLOCK, HOST_GUARD_RECOVERED]
    assert not demo_runtime.guards[-1].status.blocked


@pytest.mark.parametrize("recovery_seen_by", ["watchdog", "engine"])
def test_demo_outage_with_watchdog_restarts_sends_exactly_two_messages(
    recovery_seen_by, demo_runtime, monkeypatch, isolated_db
):
    """HT-006, demo: engine stop, watchdog restarts, outage, recovery; every Telegram call counted."""
    import watchdog

    sent = _configure_telegram(monkeypatch)
    monkeypatch.setattr(host_tolerance, "is_tradeable_session", lambda now: True)
    exits: list[int] = []
    monkeypatch.setattr(watchdog, "_ensure_mt5_running", lambda: None)
    monkeypatch.setattr(watchdog, "_get_health", lambda: None)  # the engine is not running
    monkeypatch.setattr(watchdog, "_engine_is_running", lambda: False)
    monkeypatch.setattr(watchdog, "_start_engine", lambda: exits.append(demo_runtime.start()))
    watchdog_monitor = watchdog._build_outage_monitor()

    assert demo_runtime.start() == 0
    assert len(sent) == 1 and "FX AI Engine Started" in sent[0]  # an ordinary start
    sent.clear()

    _age_snapshot(demo_runtime.bridge_dir, 45)
    demo_runtime.engines[0].run(mode="demo", iterations=1)  # its last loop, then the process ends
    watchdog._check_engine()  # restart refused: no demo snapshot
    _age_snapshot(demo_runtime.bridge_dir, 150)
    watchdog._poll_outage(watchdog_monitor)  # outage message
    watchdog._check_engine()  # still refused
    _age_snapshot(demo_runtime.bridge_dir, 0)
    if recovery_seen_by == "watchdog":
        watchdog._poll_outage(watchdog_monitor)  # recovery message
        watchdog._check_engine()  # the engine starts again
    else:
        watchdog._check_engine()  # the engine starts while the episode is open and reports the recovery
        watchdog._poll_outage(watchdog_monitor)
    assert exits == [2, 2, 0]

    assert len(sent) == 2, sent
    assert "BRIDGE OUTAGE" in sent[0] and "account_snapshot" in sent[0]
    assert "BRIDGE RECOVERED" in sent[1]
    assert [e[0] for e in _guard_events(isolated_db)] == [HOST_GUARD_BLOCK, HOST_GUARD_RECOVERED]

    assert demo_runtime.start() == 0  # a later start announces itself again
    assert len(sent) == 3 and "FX AI Engine Started" in sent[2]


def test_live_outage_past_the_stale_halt_sends_exactly_two_messages(tmp_path, runtime, isolated_db):  # noqa: F811
    """HT-006, live: reconciliation failure, live equity stale and STATE_STALE are persisted, not alerted."""
    import main

    live_gate.reset_active_session()
    sent = _configure_telegram(runtime)
    runtime.setattr(host_tolerance, "is_tradeable_session", lambda now: True)
    scenario = _live_scenario(tmp_path, runtime)
    conn = FileBridgeConnection(scenario.bridge_dir)
    assert conn.connect(retry_count=1), conn.last_error
    scenario.bridge = conn
    session = scenario.gate(env=None)
    try:
        engine = main.Engine(conn, init_tracing("test"), init_metrics(), use_mock=False, run_mode="live",
                             policy=session.policy)
        assert session.attach(engine) is True
        engine._decision_cycle = lambda: None
        engine.run(mode="live", iterations=1)
        sent.clear()  # anything the healthy start sent is not part of the outage

        def loop(snapshot_age, *, account_age=None):
            _age_snapshot(scenario.bridge_dir, snapshot_age)
            if account_age is not None:
                engine.account_status.updated_at = datetime.now(timezone.utc) - timedelta(seconds=account_age)
            engine.run(mode="live", iterations=1)

        loop(45)  # snapshot rejected: STATE_RECONCILIATION_FAILED
        loop(150, account_age=150)  # live equity stale; the outage message
        loop(200, account_age=200)  # past 180 s: STATE_STALE
        loop(0)  # the EA writes again: recovery message
    finally:
        live_gate.reset_active_session()

    assert len(sent) == 2, sent
    assert "BRIDGE OUTAGE" in sent[0] and "BRIDGE RECOVERED" in sent[1]
    blocks = {r[0] for r in _rows(isolated_db, "SELECT rule_name FROM risk_events WHERE severity IN ('BLOCK','CRITICAL')")}
    assert {"STATE_RECONCILIATION_FAILED", "LIVE_EQUITY_STALE", "STATE_STALE"} <= blocks
    infos = {r[0] for r in _rows(isolated_db, "SELECT rule_name FROM risk_events WHERE severity='INFO'")}
    assert {"STATE_RECONCILED", "STATE_RECOVERED"} <= infos


def test_stale_snapshot_outside_trading_hours_still_alerts(tmp_path, monkeypatch, isolated_db):
    sent = _configure_telegram(monkeypatch)
    monkeypatch.setattr(host_tolerance, "is_tradeable_session", lambda now: False)
    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    engine = make_file_bridge_engine(bridge_dir, monkeypatch)
    _age_snapshot(bridge_dir, 200)
    engine._insert_risk_event("STATE_STALE", "BLOCK", "Account state stale")
    assert len(sent) == 1 and "STATE_STALE" in sent[0]


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
