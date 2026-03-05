"""Watchdog for the FX AI Engine.

Checks every 5 minutes whether main.py is running.  If not, restarts it.
Designed to be scheduled via Windows Task Scheduler (every 5 minutes, at logon).

Usage:
    pythonw watchdog.py          # headless (no console window)
    python  watchdog.py          # debugging (console visible)

The watchdog writes a heartbeat log to watchdog.log next to this file.
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
ENGINE_SCRIPT = Path(__file__).parent / "main.py"
ENGINE_ARGS = ["--mode", "demo"]
LOG_FILE = Path(__file__).parent / "watchdog.log"

# Windows: CREATE_NO_WINDOW prevents a console flashing up when restarting.
CREATE_NO_WINDOW = 0x08000000


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
    """Launch main.py as a detached background process."""
    cmd = [sys.executable, str(ENGINE_SCRIPT)] + ENGINE_ARGS
    kwargs: dict = {
        "cwd": str(ENGINE_SCRIPT.parent),
        "env": os.environ.copy(),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW

    proc = subprocess.Popen(cmd, **kwargs)
    logger.info("Engine started — PID %d", proc.pid)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Watchdog started — checking every %ds", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            if not _engine_is_running():
                logger.warning("Engine not detected — restarting…")
                _start_engine()
            else:
                logger.info("Engine running — OK")
        except Exception as exc:
            logger.error("Watchdog error: %s", exc)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
