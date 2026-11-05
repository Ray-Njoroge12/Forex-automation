"""Mac host fitness check: parsers and blocking rules (no real system calls)."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import host_rules
from validation import host_check as hc

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def test_parsers():
    assert hc.parse_power_source("Now drawing from 'AC Power'\n") == "AC"
    assert hc.parse_power_source("Now drawing from 'Battery Power'\n") == "BATTERY"
    assert hc.parse_assertions("   PreventSystemSleep             1\n   PreventUserIdleSystemSleep     0\n") == {
        "PreventSystemSleep": 1, "PreventUserIdleSystemSleep": 0}
    assert hc.parse_swap_used_mb("vm.swapusage: total = 6144.00M  used = 4911.56M  free = 1232.44M") == 4911.56
    assert hc.parse_free_pct("System-wide memory free percentage: 42%") == 42
    assert hc.parse_footprint_mb("wine [58482]: 64-bit (translated) Footprint: 1630 MB (4096 bytes per page)") == 1630
    assert hc.parse_footprint_mb("x Footprint: 1.5 GB") == 1536
    assert hc.parse_ini_value("[Charts]\nMaxBars=100000000\nPreloadCharts=1\n", "MaxBars") == "100000000"
    log = "QQ\t0\t01:00:00.1\tNetwork\t'1': connection to MetaQuotes-Demo lost\nX connection to Broker-Live lost\n"
    assert hc.count_connection_drops([log, log]) == 4


def _fake_runner(*, power="AC", awake=1, nap="1", swap=500.0, free=55, mt5=True, replay=False,
                 disk_gb=120, failing_pgrep=()):
    """Fake host. ``failing_pgrep``: pgrep patterns whose probe raises ProbeError ("*" = all)."""
    def run(cmd):
        key = " ".join(cmd)
        if key == "pmset -g batt":
            return f"Now drawing from '{'AC Power' if power == 'AC' else 'Battery Power'}'\n"
        if key == "pmset -g assertions":
            return f"   PreventSystemSleep             {awake}\n   PreventUserIdleSystemSleep     0\n"
        if key.startswith("defaults read"):
            return nap + "\n"
        if key == "memory_pressure":
            return f"System-wide memory free percentage: {free}%\n"
        if key == "sysctl vm.swapusage":
            return f"vm.swapusage: total = 6144.00M  used = {swap:.2f}M  free = 1.00M\n"
        if key == "df -g /":
            return f"Filesystem 1G-blocks Used Available\n/dev/disk3s1s1 228 100 {disk_gb}\n"
        if cmd[:2] == ["pgrep", "-f"]:
            if "*" in failing_pgrep or cmd[2] in failing_pgrep:
                raise host_rules.ProbeError("pgrep: exit 3")
            if cmd[2] == "caffeinate":
                return "555\n" if awake else ""
            if cmd[2] == "terminal64.exe":
                return "58482\n" if mt5 else ""
            if replay and cmd[2] == "backtesting.run_replay":
                return "777\n"
            return ""
        if cmd[0] == "footprint":
            return "Footprint: 900 MB"
        if cmd[0] == "ps":
            return "40.0\n"
        return ""
    return run


def _mt5_dir(tmp_path: Path, *, max_bars="100000", news="0", drops=0) -> Path:
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "common.ini").write_text(f"[Charts]\nMaxBars={max_bars}\n[Server]\nNewsEnable={news}\n", encoding="utf-16")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "20261104.log").write_text("connection to MetaQuotes-Demo lost\n" * drops, encoding="utf-16")
    return tmp_path


def _bridge(tmp_path: Path, age_s: int = 5) -> Path:
    fb = tmp_path / "bridge" / "feedback"
    fb.mkdir(parents=True)
    stamp = datetime.fromtimestamp(NOW.timestamp() - age_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (fb / "account_snapshot.json").write_text(json.dumps({"timestamp": stamp}))
    return tmp_path / "bridge"


def _status(checks):
    return {c.check_id: c.status for c in checks}


def test_healthy_trading_host_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    checks = hc.run_checks("trading", _fake_runner(), _mt5_dir(tmp_path), _bridge(tmp_path), NOW)
    assert hc.BLOCK not in _status(checks).values(), checks


def test_battery_no_keepawake_app_nap_and_replay_block_trading(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    run = _fake_runner(power="BATTERY", awake=0, nap="", replay=True, swap=4911.0)
    s = _status(hc.run_checks("trading", run, _mt5_dir(tmp_path, max_bars="100000000", news="1", drops=80),
                              _bridge(tmp_path, age_s=600), NOW))
    for key in ("power_source", "sleep_prevented", "mt5_app_nap_disabled", "no_replay_jobs",
                "swap_used", "mt5_connection_drops_2d", "bridge_snapshot_fresh"):
        assert s[key] == hc.BLOCK, key
    assert s["mt5_max_bars"] == hc.WARN and s["mt5_news"] == hc.WARN


def test_research_mode_does_not_block_on_trading_only_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    run = _fake_runner(power="BATTERY", awake=0, nap="", mt5=False, replay=True)
    s = _status(hc.run_checks("research", run, _mt5_dir(tmp_path), _bridge(tmp_path, age_s=600), NOW))
    assert s["power_source"] == hc.WARN and s["sleep_prevented"] == hc.WARN
    assert s["mt5_running"] == hc.PASS and s["replay_jobs"] == hc.PASS
    assert hc.BLOCK not in s.values()


# ---------------------------------------------------------------- probe failures (fail closed)
def test_failed_pgrep_is_never_read_as_no_process(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    run = _fake_runner(failing_pgrep=("*",))
    checks = {c.check_id: c for c in hc.run_checks("trading", run, _mt5_dir(tmp_path), _bridge(tmp_path), NOW)}
    assert checks["no_replay_jobs"].status == hc.BLOCK
    assert "probe failed" in checks["no_replay_jobs"].details
    assert checks["mt5_running"].status == hc.BLOCK and checks["mt5_running"].details == "terminal64.exe probe failed"
    # PreventSystemSleep=1 still proves sleep is prevented; caffeinate is reported as unknown.
    assert checks["sleep_prevented"].status == hc.PASS
    assert "caffeinate_running=unknown" in checks["sleep_prevented"].details

    research = {c.check_id: c.status for c in hc.run_checks("research", run, _mt5_dir(tmp_path / "r"),
                                                            _bridge(tmp_path / "r"), NOW)}
    assert research["replay_jobs"] == hc.WARN


def test_one_failing_replay_pattern_blocks_the_guard(tmp_path):
    run = _fake_runner(failing_pgrep=(host_rules.REPLAY_PATTERNS[-1],))
    failed = [c.check_id for c in host_rules.blocking_failures(host_rules.guard_checks(run, _bridge(tmp_path), NOW))]
    assert failed == ["no_replay_jobs"]


@pytest.mark.parametrize("outcome", [
    OSError("pgrep not found"),
    subprocess.TimeoutExpired(["pgrep"], 30),
    SimpleNamespace(returncode=2, stdout=""),
    SimpleNamespace(returncode=3, stdout=""),
])
def test_run_command_raises_when_the_probe_did_not_run(monkeypatch, outcome):
    def fake_run(cmd, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(host_rules.subprocess, "run", fake_run)
    with pytest.raises(host_rules.ProbeError):
        host_rules.run_command(["pgrep", "-f", "research."])
    assert host_rules.read_output(host_rules.run_command, ["pmset", "-g", "batt"]) == ""


def test_run_command_reads_exit_one_as_a_result(monkeypatch):
    monkeypatch.setattr(host_rules.subprocess, "run", lambda cmd, **_k: SimpleNamespace(returncode=1, stdout=""))
    assert host_rules.run_command(["pgrep", "-f", "research."]) == ""
    assert host_rules.pids("research.", host_rules.run_command) == []


# ---------------------------------------------------------------- window-start rules (gate H1)
def test_window_start_checks_cover_every_rule_that_can_block_host_check(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    worst = _fake_runner(power="BATTERY", awake=0, nap="", swap=4911.0, free=10, mt5=False, replay=True, disk_gb=2)
    mt5_dir = _mt5_dir(tmp_path, drops=80)
    bridge = _bridge(tmp_path, age_s=600)
    cli_blocks = {c.check_id for c in hc.run_checks("trading", worst, mt5_dir, bridge, NOW) if c.status == hc.BLOCK}
    window = host_rules.window_start_checks(worst, bridge, NOW, mt5_dir)
    assert cli_blocks == {c.check_id for c in host_rules.any_blocks(window)}
    assert {"memory_free_pct", "disk_free", "mt5_running", "mt5_connection_drops_2d"} <= cli_blocks

    healthy = host_rules.window_start_checks(_fake_runner(), _bridge(tmp_path / "ok"), NOW, _mt5_dir(tmp_path / "ok"))
    assert host_rules.any_blocks(healthy) == []
    assert [c.check_id for c in healthy][-4:] == ["memory_free_pct", "disk_free", "mt5_running", "mt5_connection_drops_2d"]


@pytest.mark.parametrize("runner_kwargs,check_id", [({"free": 10}, "memory_free_pct"), ({"disk_gb": 2}, "disk_free")])
def test_memory_or_disk_block_shows_in_the_window_record_but_never_blocks_entries(tmp_path, runner_kwargs, check_id):
    window = host_rules.window_start_checks(_fake_runner(**runner_kwargs), _bridge(tmp_path), NOW, _mt5_dir(tmp_path))
    assert [c.check_id for c in host_rules.any_blocks(window)] == [check_id]
    assert host_rules.blocking_failures(window) == []
