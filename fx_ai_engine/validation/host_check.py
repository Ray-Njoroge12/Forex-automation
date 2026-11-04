"""Is this Mac fit to host the trading system right now? (read-only)

Usage (from fx_ai_engine/):
    python -m validation.host_check                 # trading mode (default)
    python -m validation.host_check --mode research # replay/research mode
    python -m validation.host_check --json

Trading mode answers "can the engine and EA run unattended without the host
silently failing?": AC power, sleep prevention, App Nap off for MT5, memory
headroom, no replay jobs, MT5 footprint and settings, bridge freshness and the
recent connection-drop rate. Research mode answers "can a replay run without
starving MT5 or the engine?".

Exit code: 0 = all PASS/WARN, 1 = at least one BLOCK. Nothing is changed.
See fx_ai_engine/docs/2026-11-04-mac-host-operating-model.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
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
REPLAY_PATTERNS = ("backtesting.run_replay", "backtesting.edge_diagnostics", "research.", "multiprocessing.spawn")

Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


@dataclass(frozen=True)
class Check:
    check_id: str
    status: str
    details: str
    fix: str = ""


# ----------------------------------------------------------------------
# Parsers (pure; unit-tested)
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


# ----------------------------------------------------------------------
# Probes
# ----------------------------------------------------------------------
def _read_utf16(path: Path) -> str:
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


def _pids(pattern: str, run: Runner) -> list[int]:
    return [int(p) for p in run(["pgrep", "-f", pattern]).split() if p.isdigit()]


def _group_footprint_mb(pattern: str, run: Runner) -> float:
    total = 0.0
    for pid in _pids(pattern, run):
        mb = parse_footprint_mb(run(["footprint", "-p", str(pid)]))
        total += max(mb, 0.0)
    return total


def _mt5_cpu(run: Runner, samples: int = 3, interval: float = 2.0) -> float:
    pids = _pids("terminal64.exe", run)
    if not pids:
        return -1.0
    values = []
    for i in range(samples):
        out = run(["ps", "-o", "pcpu=", "-p", str(pids[0])]).strip()
        try:
            values.append(float(out))
        except ValueError:
            pass
        if i + 1 < samples:
            time.sleep(interval)
    return sum(values) / len(values) if values else -1.0


def run_checks(mode: str = "trading", run: Runner = _run, mt5_data: Path = MT5_DATA,
               bridge: Path | None = None, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now(timezone.utc)
    trading = mode == "trading"
    checks: list[Check] = []

    power = parse_power_source(run(["pmset", "-g", "batt"]))
    checks.append(Check("power_source", PASS if power == "AC" else (BLOCK if trading else WARN),
                        f"power={power}", "" if power == "AC" else "Plug in the charger; macOS only blocks sleep on AC power."))

    assertions = parse_assertions(run(["pmset", "-g", "assertions"]))
    # A user-idle assertion from powerd ("display is on") vanishes when the
    # display sleeps, so only a running caffeinate or a system-sleep assertion counts.
    caffeinate = bool(_pids("caffeinate", run))
    awake = caffeinate or assertions["PreventSystemSleep"] > 0
    checks.append(Check("sleep_prevented", PASS if awake else (BLOCK if trading else WARN),
                        f"caffeinate_running={caffeinate} PreventSystemSleep={assertions['PreventSystemSleep']} "
                        f"PreventUserIdleSystemSleep={assertions['PreventUserIdleSystemSleep']}",
                        "" if awake else "Run scripts/macos/install_launchd.sh (keep-awake agent) or scripts/macos/trading_mode.sh on."))

    app_nap = run(["defaults", "read", MT5_BUNDLE_ID, "NSAppSleepDisabled"]).strip()
    nap_off = app_nap in {"1", "YES", "true"}
    checks.append(Check("mt5_app_nap_disabled", PASS if nap_off else (BLOCK if trading else WARN),
                        f"NSAppSleepDisabled={app_nap or 'unset'}",
                        "" if nap_off else f"defaults write {MT5_BUNDLE_ID} NSAppSleepDisabled -bool YES, then restart MT5."))

    free_pct = parse_free_pct(run(["memory_pressure"]))
    checks.append(Check("memory_free_pct", classify(free_pct, FREE_PCT_WARN, FREE_PCT_BLOCK, higher_is_worse=False),
                        f"free={free_pct}%", "Quit Kiro windows, browsers and replays before trading."))
    swap = parse_swap_used_mb(run(["sysctl", "vm.swapusage"]))
    checks.append(Check("swap_used", classify(swap, SWAP_WARN_MB, SWAP_BLOCK_MB),
                        f"swap_used={swap:.0f} MB",
                        "Swap only shrinks after a restart or after memory is freed; reboot before a long trading run."))

    df = run(["df", "-g", "/"]).splitlines()
    free_gb = -1.0
    if len(df) > 1:
        parts = df[1].split()
        try:
            free_gb = float(parts[3])
        except (IndexError, ValueError):
            pass
    checks.append(Check("disk_free", BLOCK if 0 <= free_gb < DISK_FREE_BLOCK_GB else PASS if free_gb >= 0 else WARN,
                        f"free={free_gb:.0f} GB"))

    replays = [p for pat in REPLAY_PATTERNS for p in _pids(pat, run)]
    checks.append(Check("no_replay_jobs" if trading else "replay_jobs",
                        (BLOCK if replays else PASS) if trading else PASS,
                        f"replay/research processes={len(replays)}",
                        "Stop replays (python -m validation.detached_job stop <job>) during trading hours." if replays and trading else ""))

    mt5_pids = _pids("terminal64.exe", run)
    if not mt5_pids:
        checks.append(Check("mt5_running", BLOCK if trading else PASS, "terminal64.exe not running",
                            "Open MetaTrader 5.app and attach FX_Execution." if trading else ""))
    else:
        mt5_mb = _group_footprint_mb("terminal64.exe", run)
        checks.append(Check("mt5_footprint", WARN if mt5_mb > MT5_FOOTPRINT_WARN_MB else PASS, f"footprint={mt5_mb:.0f} MB",
                            "Lower Tools > Options > Charts > Max bars in chart to 100000, close extra charts, hide unused Market Watch symbols."))
        cpu = _mt5_cpu(run)
        checks.append(Check("mt5_cpu", WARN if cpu > MT5_CPU_WARN_PCT else PASS, f"cpu={cpu:.0f}% (avg of 3 samples)",
                            "Minimise chart redraw: one chart, no indicators, News off; see doc section 4.2."))

    common = _read_utf16(mt5_data / "config" / "common.ini")
    max_bars = parse_ini_value(common, "MaxBars")
    news = parse_ini_value(common, "NewsEnable")
    try:
        max_bars_n = int(max_bars) if max_bars else -1
    except ValueError:
        max_bars_n = -1
    checks.append(Check("mt5_max_bars", WARN if max_bars_n > MAXBARS_WARN else PASS, f"MaxBars={max_bars}",
                        "Set Max bars in chart to 100000 (Unlimited was only needed for the history export)."))
    checks.append(Check("mt5_news", WARN if news == "1" else PASS, f"NewsEnable={news}",
                        "Tools > Options > Server: untick Enable news (the engine uses its own calendar)."))

    logs = []
    for day in (now, now - timedelta(days=1)):
        logs.append(_read_utf16(mt5_data / "logs" / f"{day:%Y%m%d}.log"))
    drops = count_connection_drops(logs)
    checks.append(Check("mt5_connection_drops_2d", classify(drops, DROPS_WARN_24H * 2, DROPS_BLOCK_24H * 2),
                        f"drops today+yesterday={drops}",
                        "Most drops coincide with system sleep/DarkWake; fix sleep and App Nap first, then check Wi-Fi."))

    kiro = _group_footprint_mb("Kiro Helper|Kiro.app", run)
    browsers = _group_footprint_mb("Brave|Google Chrome|Safari|firefox", run)
    if trading:
        checks.append(Check("kiro_footprint", WARN if kiro > KIRO_TRADING_WARN_MB else PASS, f"Kiro={kiro:.0f} MB",
                            "Close extra Kiro windows and pause workflows/agent sessions while trading."))
        checks.append(Check("browser_footprint", WARN if browsers > BROWSER_TRADING_WARN_MB else PASS, f"browsers={browsers:.0f} MB"))

    bridge = bridge or (Path(os.environ["BRIDGE_BASE_PATH"]) if os.getenv("BRIDGE_BASE_PATH") else mt5_data / "MQL5/Files/bridge")
    snap = bridge / "feedback" / "account_snapshot.json"
    try:
        stamp = datetime.fromisoformat(json.loads(snap.read_text())["timestamp"].replace("Z", "+00:00"))
        age = (now - stamp).total_seconds()
        status = PASS if age <= 30 else (BLOCK if trading else WARN)
        checks.append(Check("bridge_snapshot_fresh", status, f"age={age:.0f}s",
                            "" if status == PASS else "The EA is not writing snapshots: MT5 closed, asleep or EA detached."))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        checks.append(Check("bridge_snapshot_fresh", BLOCK if trading else WARN, f"unreadable: {type(exc).__name__}"))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mac host fitness check (read-only)")
    parser.add_argument("--mode", choices=("trading", "research"), default="trading")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        from core.env_loader import load_runtime_env
        load_runtime_env()
    except Exception:
        pass
    checks = run_checks(args.mode)
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for c in checks:
            line = f"{c.status:5} {c.check_id}: {c.details}"
            print(line + (f"\n      fix: {c.fix}" if c.fix and c.status != PASS else ""))
        blocks = sum(c.status == BLOCK for c in checks)
        warns = sum(c.status == WARN for c in checks)
        print(f"\nmode={args.mode}: {blocks} BLOCK, {warns} WARN, {len(checks) - blocks - warns} PASS")
    return 1 if any(c.status == BLOCK for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
