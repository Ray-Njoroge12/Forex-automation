from __future__ import annotations

import os
import logging
from pathlib import Path
import MetaTrader5 as mt5

logger = logging.getLogger("fx_ai_engine.bridge_utils")

def get_mt5_bridge_path() -> Path:
    """Detects the MT5 MQL5/Files folder and returns the bridge path.
    
    If BRIDGE_BASE_PATH is set in env, uses that.
    Otherwise, initializes MT5 and asks for data_path.
    Fallback: current project bridge directory.
    """
    env_path = os.getenv("BRIDGE_BASE_PATH")
    if env_path:
        return Path(env_path)

    # Attempt to auto-detect from active MT5
    if mt5.initialize():
        terminal_info = mt5.terminal_info()
        if terminal_info:
            data_path = Path(terminal_info.data_path)
            bridge_path = data_path / "MQL5" / "Files" / "bridge"
            mt5.shutdown()
            return bridge_path
        mt5.shutdown()

    # Generic fallback to local project folder
    return Path(__file__).parent.parent / "bridge"
