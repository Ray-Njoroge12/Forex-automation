"""Archive the EA's MT5 calendar export and install it through the validated installer.

Run daily (launchd agent com.fxai.calendar) from fx_ai_engine/:
    python -m validation.refresh_mt5_calendar

The running engine loads the calendar at start-up; it picks up the new file on
its next restart. The installed file must cover at least 7 days ahead.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from core.env_loader import load_runtime_env
from validation.update_market_context import DATA_DIR, install_calendar


def main() -> int:
    load_runtime_env()
    bridge = os.getenv("BRIDGE_BASE_PATH")
    if not bridge:
        raise SystemExit("BRIDGE_BASE_PATH is not set")
    source = Path(bridge) / "market" / "calendar.json"
    if not source.is_file():
        raise SystemExit(f"calendar export missing: {source}")
    now = datetime.now(timezone.utc)
    archived = DATA_DIR / "source_records" / f"market_context_{now:%Y-%m-%d}_mt5_calendar.json"
    shutil.copy2(source, archived)
    install_calendar(archived, now_utc=now)
    print(f"{now.isoformat()} installed {archived.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
