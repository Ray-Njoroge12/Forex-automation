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

The rules live in ``core.host_rules`` and are shared with the engine host
guard, which re-evaluates the fast subset (``core.host_rules.guard_checks``)
while it runs and refuses new entries on a BLOCK, and records every rule that
can BLOCK here (``core.host_rules.window_start_checks``) at the start of each
trading window for gate H1. A probe command that fails to run is never read as
a pass: a failed ``pgrep`` makes the replay rule BLOCK in trading mode.

Exit code: 0 = all PASS/WARN, 1 = at least one BLOCK. Nothing is changed.
See fx_ai_engine/docs/2026-11-04-mac-host-operating-model.md.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from core.host_rules import (  # noqa: F401  (re-exported for callers and tests)
    BLOCK,
    BROWSER_TRADING_WARN_MB,
    DISK_FREE_BLOCK_GB,
    DROPS_BLOCK_24H,
    DROPS_WARN_24H,
    FREE_PCT_BLOCK,
    FREE_PCT_WARN,
    KIRO_TRADING_WARN_MB,
    MAXBARS_WARN,
    MT5_BUNDLE_ID,
    MT5_CPU_WARN_PCT,
    MT5_DATA,
    MT5_FOOTPRINT_WARN_MB,
    PASS,
    REPLAY_PATTERNS,
    SWAP_BLOCK_MB,
    SWAP_WARN_MB,
    WARN,
    Check,
    ProbeError,
    Runner,
    app_nap_check,
    classify,
    count_connection_drops,
    default_bridge_path,
    disk_check,
    drops_check,
    memory_check,
    mt5_pids,
    mt5_running_check,
    parse_assertions,
    parse_footprint_mb,
    parse_free_pct,
    parse_ini_value,
    parse_power_source,
    parse_swap_used_mb,
    pids as _pids,
    power_check,
    read_output,
    read_utf16 as _read_utf16,
    replay_check,
    run_command as _run,
    sleep_check,
    snapshot_check,
    swap_check,
)


def _group_footprint_mb(pattern: str, run: Runner) -> float:
    try:
        found = _pids(pattern, run)
    except ProbeError:
        return 0.0
    total = 0.0
    for pid in found:
        mb = parse_footprint_mb(read_output(run, ["footprint", "-p", str(pid)]))
        total += max(mb, 0.0)
    return total


def _mt5_cpu(run: Runner, samples: int = 3, interval: float = 2.0) -> float:
    pids = mt5_pids(run)
    if not pids:
        return -1.0
    values = []
    for i in range(samples):
        out = read_output(run, ["ps", "-o", "pcpu=", "-p", str(pids[0])]).strip()
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
    checks: list[Check] = [
        power_check(run, trading=trading),
        sleep_check(run, trading=trading),
        app_nap_check(run, trading=trading),
    ]

    checks.append(memory_check(run))
    checks.append(swap_check(run))
    checks.append(disk_check(run))
    checks.append(replay_check(run, trading=trading))

    found = mt5_pids(run)
    if not found:
        checks.append(mt5_running_check(found, trading=trading))
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

    checks.append(drops_check(mt5_data, now))

    kiro = _group_footprint_mb("Kiro Helper|Kiro.app", run)
    browsers = _group_footprint_mb("Brave|Google Chrome|Safari|firefox", run)
    if trading:
        checks.append(Check("kiro_footprint", WARN if kiro > KIRO_TRADING_WARN_MB else PASS, f"Kiro={kiro:.0f} MB",
                            "Close extra Kiro windows and pause workflows/agent sessions while trading."))
        checks.append(Check("browser_footprint", WARN if browsers > BROWSER_TRADING_WARN_MB else PASS, f"browsers={browsers:.0f} MB"))

    checks.append(snapshot_check(bridge or default_bridge_path(mt5_data), now, trading=trading))
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
