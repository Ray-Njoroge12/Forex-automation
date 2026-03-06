from __future__ import annotations

import os
from pathlib import Path

import MetaTrader5 as mt5


def check_paths():
    if not mt5.initialize():
        print("initialize() failed, error code =", mt5.last_error())
        quit()

    terminal_info = mt5.terminal_info()
    if terminal_info:
        print(f"Data Path: {terminal_info.data_path}")
        print(f"Common Path: {terminal_info.commondatapath}")

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


if __name__ == "__main__":
    check_paths()
