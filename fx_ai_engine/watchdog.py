"""Watchdog for the FX AI Engine.

Checks every 5 minutes whether main.py is running.  If not, restarts it.
Designed to be scheduled via Windows Task Scheduler (every 5 minutes, at logon).

Usage:
    pythonw watchdog.py          # headless (no console window)
    python  watchdog.py          # debugging (console visible)

The watchdog writes a heartbeat log to watchdog.log next to this file.

It also polls the bridge outage alert (application.host_tolerance.OutageMonitor)
every 30 seconds. The engine stops when the account snapshot goes stale, so the
watchdog is the process that is still running to report an outage; both share
the episode state file, so each outage sends one message and one recovery.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CHECK_INTERVAL_SECONDS = 300   # 5 minutes — match Task Scheduler trigger
OUTAGE_POLL_SECONDS = 30       # bridge outage alert (two messages per outage)
ENGINE_SCRIPT = Path(__file__).parent / "main.py"
ENGINE_ARGS = ["--mode", "demo"]
LOG_FILE = Path(__file__).parent / "watchdog.log"
HEALTH_URL = f"http://localhost:{os.getenv('HEALTH_PORT', '8080')}/health"

# Windows: CREATE_NO_WINDOW prevents a console flashing up when restarting.
CREATE_NO_WINDOW = 0x08000000

import json
import urllib.request
from datetime import datetime, timezone
import psutil

# ---------------------------------------------------------------------------
# Health monitoring
# ---------------------------------------------------------------------------

def _get_health() -> dict | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as response:
            if response.status == 200:
                return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        logger.debug("Health endpoint unavailable", exc_info=True)
    return None

def _is_health_stale(health: dict) -> bool:
    hb_str = health.get("last_heartbeat")
    if not hb_str:
        return True
    try:
        hb_time = datetime.fromisoformat(hb_str)
        if hb_time.tzinfo is None:
            hb_time = hb_time.replace(tzinfo=timezone.utc)
        # If heartbeat is older than 10 minutes, it's stale.
        diff = datetime.now(timezone.utc) - hb_time.astimezone(timezone.utc)
        return diff.total_seconds() > 600
    except (TypeError, ValueError):
        return True

def _get_mt5_path() -> str | None:
    for proc in psutil.process_iter(["name", "exe"]):
        if proc.info["name"] == "terminal64.exe":
            return proc.info["exe"]
    return None

def _ensure_mt5_running() -> None:
    if sys.platform == "darwin":
        # MT5 runs inside Wine; the process command line contains terminal64.exe.
        for proc in psutil.process_iter(["cmdline"]):
            try:
                if any("terminal64.exe" in arg for arg in (proc.info.get("cmdline") or [])):
                    return
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        logger.warning("MT5 terminal not detected — opening MetaTrader 5.app")
        try:
            subprocess.Popen(["open", "-a", "MetaTrader 5"])
        except OSError as e:
            logger.error("Failed to open MetaTrader 5.app: %s", e)
        return
    path = _get_mt5_path()
    if path:
        # It's running, good. We could save this path for later.
        with (Path(__file__).parent / ".mt5_path").open("w", encoding="utf-8") as f:
            f.write(path)
        return
    
    # Not running. Check if we have a saved path.
    saved_path_file = Path(__file__).parent / ".mt5_path"
    if saved_path_file.exists():
        with saved_path_file.open("r", encoding="utf-8") as f:
            saved_path = f.read().strip()
        if os.path.exists(saved_path):
            logger.warning("MT5 terminal not detected — attempting to restart from %s", saved_path)
            try:
                subprocess.Popen([saved_path], creationflags=CREATE_NO_WINDOW)
            except OSError as e:
                logger.error("Failed to restart MT5: %s", e)
    else:
        logger.error("MT5 terminal not detected and no saved path found.")

# ---------------------------------------------------------------------------
# Logging — rotate-free single file (simple for local use)
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("watchdog")


# ---------------------------------------------------------------------------
# Process detection
# ---------------------------------------------------------------------------

def _engine_is_running() -> bool:
    """Return True if any python process is running main.py."""
    try:
        import psutil  # type: ignore[import]
    except ImportError:
        logger.error("psutil not installed — run: pip install psutil")
        return True  # assume running to avoid accidental double-start

    target = str(ENGINE_SCRIPT.resolve())
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if any(target in arg for arg in cmdline):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

def _start_engine() -> None:
    """Launch main.py as a detached background process with output redirection."""
    engine_log = Path(__file__).parent / "engine_stdout.log"
    # Append mode for logs
    log_file = open(engine_log, "a", encoding="utf-8")
    
    cmd = [sys.executable, str(ENGINE_SCRIPT)] + ENGINE_ARGS
    kwargs: dict = {
        "cwd": str(ENGINE_SCRIPT.parent),
        "env": os.environ.copy(),
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW

    proc = subprocess.Popen(cmd, **kwargs)
    logger.info("Engine started — PID %d (logging to %s)", proc.pid, engine_log)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _check_engine() -> None:
    # First, ensure MT5 is up
    _ensure_mt5_running()

    health = _get_health()
    if not health:
        if not _engine_is_running():
            logger.warning("Engine not detected — restarting…")
            _start_engine()
        else:
            logger.warning("Engine detected but health check failed — waiting for next cycle")
    elif _is_health_stale(health):
        logger.warning("Engine health is stale — attempting to restart engine process")
        # Kill existing engine processes
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if any(str(ENGINE_SCRIPT.resolve()) in arg for arg in cmdline):
                    proc.kill()
                    logger.info("Killed stale engine process (PID %d)", proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _start_engine()
    else:
        logger.info("Engine healthy and running — OK")


def _build_outage_monitor():
    """The engine's outage monitor for the real bridge; None in mock mode or without a bridge."""
    try:
        from application import host_tolerance
        from core.bridge_utils import get_mt5_bridge_path
        from core.env_loader import load_runtime_env

        load_runtime_env()  # Telegram settings live in fx_ai_engine/.env (never overrides set vars)
        if os.getenv("USE_MT5_MOCK", "0") == "1":
            return None
        return host_tolerance.build_outage_monitor(use_mock=False, bridge_path=get_mt5_bridge_path())
    except Exception as exc:
        logger.error("Outage alert unavailable: %s", exc)
        return None


def _poll_outage(monitor) -> None:
    if monitor is None:
        return
    try:
        event = monitor.poll(datetime.now(timezone.utc))
        if event:
            logger.warning("Bridge outage alert: %s", event)
    except Exception as exc:
        logger.error("Outage alert poll failed: %s", exc)


def main() -> None:
    logger.info("Watchdog started — checking every %ds", CHECK_INTERVAL_SECONDS)
    monitor = _build_outage_monitor()
    last_engine_check: float | None = None
    while True:
        if last_engine_check is None or time.monotonic() - last_engine_check >= CHECK_INTERVAL_SECONDS:
            last_engine_check = time.monotonic()
            try:
                _check_engine()
            except Exception as exc:
                logger.error("Watchdog error: %s", exc)
        _poll_outage(monitor)
        time.sleep(OUTAGE_POLL_SECONDS)


if __name__ == "__main__":
    main()
