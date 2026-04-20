from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import MetaTrader5 as mt5  # type: ignore
except Exception:
    mt5 = None


def check_paths() -> int:
    if mt5 is None:
        print("MetaTrader5 package is unavailable in this environment.")
        return 1

    timeout_raw = os.getenv("MT5_INIT_TIMEOUT_MS", "15000").strip()
    try:
        timeout_ms = max(1000, int(timeout_raw))
    except ValueError:
        timeout_ms = 15000

    terminal_path = os.getenv("MT5_TERMINAL_PATH", "").strip()

    init_kwargs = {"timeout": timeout_ms}
    if terminal_path:
        init_kwargs["path"] = terminal_path

    try:
        init_ok = mt5.initialize(**init_kwargs)
    except TypeError:
        # Compatibility for wrappers not exposing timeout/path kwargs.
        init_ok = mt5.initialize()

    if not init_ok:
        print("initialize() failed, error code =", mt5.last_error())
        return 1

    terminal_info = mt5.terminal_info()
    if terminal_info:
        common_path = getattr(terminal_info, "commondata_path", None)
        if common_path is None:
            # Backward compatibility with older wrappers exposing a legacy attribute.
            common_path = getattr(terminal_info, "commondatapath", "<unavailable>")

        print(f"Data Path: {terminal_info.data_path}")
        print(f"Common Path: {common_path}")

        expected_bridge = Path(terminal_info.data_path) / "MQL5" / "Files" / "bridge"
        print(f"Expected bridge path: {expected_bridge}")

    mt5.shutdown()

    print("\n--- BRIDGE_BASE_PATH guidance ---")
    env_path = os.getenv("BRIDGE_BASE_PATH", "")
    if env_path:
        print(f"BRIDGE_BASE_PATH is SET: {env_path}")
        if not Path(env_path).exists():
            print("  WARNING: Path does not exist! Check spelling.")
        else:
            print("  Path exists and is reachable.")
    else:
        print("BRIDGE_BASE_PATH is NOT SET.")
        print("The engine will try to auto-detect from MT5 terminal_info.")
        print("If STATE_STALE errors occur, set this manually in .env:")
        print("  Find it in MT5: Tools > Options > Files > 'Open data folder'")
        print(r"  Then append: \MQL5\Files\bridge")
        print(r"  Example: BRIDGE_BASE_PATH=C:\Users\You\AppData\Roaming\MetaQuotes\Terminal\<hash>\MQL5\Files\bridge")

    return 0


if __name__ == "__main__":
    sys.exit(check_paths())
