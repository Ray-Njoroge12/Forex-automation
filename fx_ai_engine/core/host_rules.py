"""Mac host rules shared by ``validation.host_check`` (CLI) and the engine host guard.

Pure parsers and rule functions plus cheap probes. Every probe takes a
``Runner`` (command list -> stdout) so tests never touch the real host. A
runner raises ``ProbeError`` when a command could not run; process probes
(``pids``) let it propagate so a failed ``pgrep`` is never read as "no process",
and the other probes read through ``read_output``, which turns it into empty output.

``guard_checks`` is the fast trading-mode subset the engine evaluates while it
runs: no MT5 CPU sampling, no ``footprint`` calls, one short subprocess per probe.
``window_start_checks`` adds the other rules that can BLOCK in ``host_check``
trading mode (memory, disk, MT5 running, connection drops); the engine records it
once at the start of each trading window for gate H1.
See fx_ai_engine/docs/2026-11-04-mac-host-operating-model.md.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

PASS, WARN, BLOCK = "PASS", "WARN", "BLOCK"
MT5_BUNDLE_ID = "net.metaquotes.wine.MetaTrader5"
MT5_DATA = Path.home() / "Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5"

# Budgets for an 8 GB Apple Silicon Mac (see the operating-model doc, section 3).
SWAP_WARN_MB, SWAP_BLOCK_MB = 1500, 4000
FREE_PCT_WARN, FREE_PCT_BLOCK = 30, 15
MT5_FOOTPRINT_WARN_MB = 1300
MT5_CPU_WARN_PCT = 90
KIRO_TRADING_WARN_MB = 1500
BROWSER_TRADING_WARN_MB = 600
DISK_FREE_BLOCK_GB = 5
DROPS_WARN_24H, DROPS_BLOCK_24H = 6, 30
MAXBARS_WARN = 1_000_000
SNAPSHOT_FRESH_SECONDS = 30
REPLAY_PATTERNS = ("backtesting.run_replay", "backtesting.edge_diagnostics", "research.", "multiprocessing.spawn")

# The rules that make the engine refuse new entries (backlog item 1).
GUARD_BLOCKING_CHECKS = (
    "power_source",
    "sleep_prevented",
    "mt5_app_nap_disabled",
    "no_replay_jobs",
    "bridge_snapshot_fresh",
)

Runner = Callable[[list[str]], str]


class ProbeError(RuntimeError):
    """A host probe command could not run, timed out or reported an error."""


def run_command(cmd: list[str], timeout: float = 30) -> str:
    """stdout of *cmd*. Exit 0 or 1 is a result (pgrep: match / no match, defaults: key unset);
    a failure to run, a timeout, exit >= 2 or a signal kill (negative code) raises ``ProbeError``."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"{cmd[0]}: {type(exc).__name__}") from exc
    if proc.returncode < 0:  # killed by a signal: the output is not a result
        raise ProbeError(f"{cmd[0]}: killed by signal {-proc.returncode}")
    if proc.returncode > 1:
        raise ProbeError(f"{cmd[0]}: exit {proc.returncode}")
    return proc.stdout


def read_output(run: Runner, cmd: list[str]) -> str:
    """Best-effort stdout: empty when the probe failed (the rules treat empty output as unknown)."""
    try:
        return run(cmd)
    except ProbeError:
        return ""


@dataclass(frozen=True)
class Check:
    check_id: str
    status: str
    details: str
    fix: str = ""


# ----------------------------------------------------------------------
# Parsers (pure)
# ----------------------------------------------------------------------
def parse_power_source(pmset_batt: str) -> str:
    if "AC Power" in pmset_batt:
        return "AC"
    if "Battery Power" in pmset_batt:
        return "BATTERY"
    return "UNKNOWN"


def parse_assertions(pmset_assertions: str) -> dict[str, int]:
    out = {}
    for key in ("PreventSystemSleep", "PreventUserIdleSystemSleep"):
        m = re.search(rf"^\s*{key}\s+(\d+)", pmset_assertions, re.MULTILINE)
        out[key] = int(m.group(1)) if m else 0
    return out


def parse_swap_used_mb(swapusage: str) -> float:
    m = re.search(r"used = ([\d.]+)M", swapusage)
    return float(m.group(1)) if m else -1.0


def parse_free_pct(memory_pressure: str) -> int:
    m = re.search(r"free percentage:\s*(\d+)%", memory_pressure)
    return int(m.group(1)) if m else -1


def parse_footprint_mb(footprint: str) -> float:
    m = re.search(r"Footprint:\s*([\d.]+)\s*(KB|MB|GB)", footprint)
    if not m:
        return -1.0
    value, unit = float(m.group(1)), m.group(2)
    return value / 1024 if unit == "KB" else value * 1024 if unit == "GB" else value


def parse_ini_value(text: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}=(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def count_connection_drops(log_texts: list[str]) -> int:
    return sum(len(re.findall(r"connection to \S+ lost", text)) for text in log_texts)


def classify(value: float, warn: float, block: float, *, higher_is_worse: bool = True) -> str:
    if value < 0:
        return WARN
    if higher_is_worse:
        return BLOCK if value >= block else WARN if value >= warn else PASS
    return BLOCK if value <= block else WARN if value <= warn else PASS


def read_utf16(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    for enc in ("utf-16", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def pids(pattern: str, run: Runner) -> list[int]:
    """Process ids matching *pattern*; raises ``ProbeError`` when pgrep itself failed."""
    return [int(p) for p in run(["pgrep", "-f", pattern]).split() if p.isdigit()]


def default_bridge_path(mt5_data: Path = MT5_DATA) -> Path:
    if os.getenv("BRIDGE_BASE_PATH"):
        return Path(os.environ["BRIDGE_BASE_PATH"])
    return mt5_data / "MQL5/Files/bridge"


# ----------------------------------------------------------------------
# Rules (one Check each; trading=True applies the trading-mode BLOCKs)
# ----------------------------------------------------------------------
def power_check(run: Runner, *, trading: bool) -> Check:
    power = parse_power_source(read_output(run, ["pmset", "-g", "batt"]))
    return Check("power_source", PASS if power == "AC" else (BLOCK if trading else WARN),
                 f"power={power}", "" if power == "AC" else "Plug in the charger; macOS only blocks sleep on AC power.")


def sleep_check(run: Runner, *, trading: bool) -> Check:
    assertions = parse_assertions(read_output(run, ["pmset", "-g", "assertions"]))
    # A user-idle assertion from powerd ("display is on") vanishes when the
    # display sleeps, so only a running caffeinate or a system-sleep assertion counts.
    try:
        caffeinate: bool | None = bool(pids("caffeinate", run))
    except ProbeError:
        caffeinate = None  # unknown counts as not running
    awake = bool(caffeinate) or assertions["PreventSystemSleep"] > 0
    return Check("sleep_prevented", PASS if awake else (BLOCK if trading else WARN),
                 f"caffeinate_running={'unknown' if caffeinate is None else caffeinate} PreventSystemSleep={assertions['PreventSystemSleep']} "
                 f"PreventUserIdleSystemSleep={assertions['PreventUserIdleSystemSleep']}",
                 "" if awake else "Run scripts/macos/install_launchd.sh (keep-awake agent) or scripts/macos/trading_mode.sh on.")


def app_nap_check(run: Runner, *, trading: bool) -> Check:
    app_nap = read_output(run, ["defaults", "read", MT5_BUNDLE_ID, "NSAppSleepDisabled"]).strip()
    nap_off = app_nap in {"1", "YES", "true"}
    return Check("mt5_app_nap_disabled", PASS if nap_off else (BLOCK if trading else WARN),
                 f"NSAppSleepDisabled={app_nap or 'unset'}",
                 "" if nap_off else f"defaults write {MT5_BUNDLE_ID} NSAppSleepDisabled -bool YES, then restart MT5.")


def swap_check(run: Runner) -> Check:
    swap = parse_swap_used_mb(read_output(run, ["sysctl", "vm.swapusage"]))
    return Check("swap_used", classify(swap, SWAP_WARN_MB, SWAP_BLOCK_MB),
                 f"swap_used={swap:.0f} MB",
                 "Swap only shrinks after a restart or after memory is freed; reboot before a long trading run.")


def replay_check(run: Runner, *, trading: bool) -> Check:
    check_id = "no_replay_jobs" if trading else "replay_jobs"
    try:
        replays = [p for pat in REPLAY_PATTERNS for p in pids(pat, run)]
    except ProbeError as exc:  # fail closed: an unknown process table is not "no replays"
        return Check(check_id, BLOCK if trading else WARN, f"replay/research process probe failed: {exc}",
                     "pgrep did not run; check the host before trading.")
    return Check("no_replay_jobs" if trading else "replay_jobs",
                 (BLOCK if replays else PASS) if trading else PASS,
                 f"replay/research processes={len(replays)}",
                 "Stop replays (python -m validation.detached_job stop <job>) during trading hours." if replays and trading else "")


def snapshot_age_seconds(path: Path, now: datetime, key: str = "timestamp") -> float | None:
    """Age of the ISO timestamp *key* in the JSON file at *path*; None if missing or unreadable."""
    try:
        stamp = datetime.fromisoformat(str(json.loads(path.read_text())[key]).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds()


def snapshot_check(bridge: Path, now: datetime, *, trading: bool) -> Check:
    snap = bridge / "feedback" / "account_snapshot.json"
    try:
        stamp = datetime.fromisoformat(json.loads(snap.read_text())["timestamp"].replace("Z", "+00:00"))
        age = (now - stamp).total_seconds()
        status = PASS if age <= SNAPSHOT_FRESH_SECONDS else (BLOCK if trading else WARN)
        return Check("bridge_snapshot_fresh", status, f"age={age:.0f}s",
                     "" if status == PASS else "The EA is not writing snapshots: MT5 closed, asleep or EA detached.")
    except (OSError, ValueError, KeyError, AttributeError, TypeError, json.JSONDecodeError) as exc:
        return Check("bridge_snapshot_fresh", BLOCK if trading else WARN, f"unreadable: {type(exc).__name__}")


def memory_check(run: Runner) -> Check:
    free_pct = parse_free_pct(read_output(run, ["memory_pressure"]))
    return Check("memory_free_pct", classify(free_pct, FREE_PCT_WARN, FREE_PCT_BLOCK, higher_is_worse=False),
                 f"free={free_pct}%", "Quit Kiro windows, browsers and replays before trading.")


def disk_check(run: Runner) -> Check:
    df = read_output(run, ["df", "-g", "/"]).splitlines()
    free_gb = -1.0
    if len(df) > 1:
        parts = df[1].split()
        try:
            free_gb = float(parts[3])
        except (IndexError, ValueError):
            pass
    return Check("disk_free", BLOCK if 0 <= free_gb < DISK_FREE_BLOCK_GB else PASS if free_gb >= 0 else WARN,
                 f"free={free_gb:.0f} GB")


def mt5_pids(run: Runner) -> list[int] | None:
    """terminal64.exe process ids; None when the probe failed."""
    try:
        return pids("terminal64.exe", run)
    except ProbeError:
        return None


def mt5_running_check(found: list[int] | None, *, trading: bool) -> Check:
    if found:
        return Check("mt5_running", PASS, f"terminal64.exe running (pid {found[0]})")
    details = "terminal64.exe probe failed" if found is None else "terminal64.exe not running"
    return Check("mt5_running", BLOCK if trading else PASS, details,
                 "Open MetaTrader 5.app and attach FX_Execution." if trading else "")


def drops_check(mt5_data: Path, now: datetime) -> Check:
    logs = [read_utf16(mt5_data / "logs" / f"{day:%Y%m%d}.log") for day in (now, now - timedelta(days=1))]
    drops = count_connection_drops(logs)
    return Check("mt5_connection_drops_2d", classify(drops, DROPS_WARN_24H * 2, DROPS_BLOCK_24H * 2),
                 f"drops today+yesterday={drops}",
                 "Most drops coincide with system sleep/DarkWake; fix sleep and App Nap first, then check Wi-Fi.")


def guard_checks(run: Runner = run_command, bridge: Path | None = None, now: datetime | None = None) -> list[Check]:
    """Trading-mode rules the engine host guard evaluates (cheap: about nine short commands).

    Blocking ids are ``GUARD_BLOCKING_CHECKS``; ``swap_used`` is recorded for gate H1 only.
    """
    now = now or datetime.now(timezone.utc)
    bridge = bridge or default_bridge_path()
    return [
        power_check(run, trading=True),
        sleep_check(run, trading=True),
        app_nap_check(run, trading=True),
        replay_check(run, trading=True),
        snapshot_check(bridge, now, trading=True),
        swap_check(run),
    ]


def window_start_checks(
    run: Runner = run_command,
    bridge: Path | None = None,
    now: datetime | None = None,
    mt5_data: Path = MT5_DATA,
) -> list[Check]:
    """``guard_checks`` plus every other rule that can BLOCK in ``host_check`` trading mode.

    Left out are the WARN-only rules (MT5 and app footprints, MT5 CPU, Max bars,
    news): they cannot block, and sampling MT5 CPU takes seconds. Recorded once at
    the start of each trading window for gate H1; entries still block only on
    ``GUARD_BLOCKING_CHECKS``.
    """
    now = now or datetime.now(timezone.utc)
    return [
        *guard_checks(run, bridge, now),
        memory_check(run),
        disk_check(run),
        mt5_running_check(mt5_pids(run), trading=True),
        drops_check(mt5_data, now),
    ]


def blocking_failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.check_id in GUARD_BLOCKING_CHECKS and c.status == BLOCK]


def any_blocks(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status == BLOCK]
