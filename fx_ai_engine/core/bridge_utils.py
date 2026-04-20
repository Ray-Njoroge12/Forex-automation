from __future__ import annotations

import os
import logging
import re
from pathlib import Path

mt5 = None
try:
    import MetaTrader5 as mt5  # type: ignore
except Exception:
    mt5 = None

logger = logging.getLogger("fx_ai_engine.bridge_utils")


def _default_mock_bridge_path() -> Path:
    return Path(__file__).resolve().parent.parent / "mock_mt5_bridge"


def _is_mock_mode() -> bool:
    return os.getenv("USE_MT5_MOCK") == "1"


def _is_windows_runtime() -> bool:
    return os.name == "nt"


def _coerce_bridge_path(raw_path: str) -> Path:
    windows_match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw_path)
    if windows_match and not _is_windows_runtime():
        drive = windows_match.group(1).lower()
        remainder = windows_match.group(2).replace("\\", "/")
        return Path(f"/mnt/{drive}/{remainder}")
    return Path(raw_path)


def _current_policy_mode() -> str:
    explicit = os.getenv("FX_POLICY_MODE", "").strip().lower()
    if explicit:
        return explicit
    return "core_srs"


def get_mock_runtime_state_path(base_path: Path | None = None) -> Path:
    root = base_path if base_path is not None else get_mt5_bridge_path()
    mode = re.sub(r"[^a-z0-9_\-]+", "_", _current_policy_mode())
    return root / f"mock_runtime_state.{mode}.json"

def get_mt5_bridge_path() -> Path:
    """Detects the MT5 MQL5/Files folder and returns the bridge path.

    Resolution order:
    1) Mock mode (`USE_MT5_MOCK=1`): dedicated mock bridge path.
    2) Explicit env override (`BRIDGE_BASE_PATH`).
    3) MT5 terminal auto-detection via `terminal_info().data_path`.

    Live mode is fail-closed: when no explicit or auto-detected path is
    available, this function raises RuntimeError instead of falling back to a
    local project directory.
    """
    if _is_mock_mode():
        mock_env_path = os.getenv("MT5_MOCK_BRIDGE_PATH")
        return _coerce_bridge_path(mock_env_path) if mock_env_path else _default_mock_bridge_path()

    env_path = os.getenv("BRIDGE_BASE_PATH")
    if env_path:
        return _coerce_bridge_path(env_path)

    # Attempt to auto-detect from active MT5
    if mt5 is not None:
        initialized = False
        try:
            initialized = bool(mt5.initialize())
            if initialized:
                terminal_info = mt5.terminal_info()
                if terminal_info and getattr(terminal_info, "data_path", None):
                    data_path = Path(terminal_info.data_path)
                    bridge_path = data_path / "MQL5" / "Files" / "bridge"
                    return bridge_path
        finally:
            if initialized:
                mt5.shutdown()

    raise RuntimeError(
        "Unable to resolve live MT5 bridge path. "
        "Set BRIDGE_BASE_PATH to your MT5 MQL5/Files/bridge directory "
        "or ensure MT5 terminal auto-detection is available."
    )
