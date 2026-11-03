"""Read-only diagnostics for a background MT5 connection on this machine.

No account connection, terminal launch, credentials or order submission occurs.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
from pathlib import Path
from typing import Any


def probe(*, home: Path | None = None, applications: Path = Path('/Applications'),
          platform_name: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    home = home or Path.home()
    platform_name = platform_name or platform.system()
    env = os.environ if env is None else env
    app = applications / 'MetaTrader 5.app'
    prefix = home / 'Library/Application Support/net.metaquotes.wine.metatrader5/drive_c'
    terminal_root = prefix / 'Program Files/MetaTrader 5'
    candidates = []
    if (terminal_root / 'MQL5').is_dir():
        candidates.append(terminal_root / 'MQL5')
    wine_users = prefix / 'users'
    if wine_users.is_dir():
        candidates.extend(path for path in wine_users.glob('*/AppData/Roaming/MetaQuotes/Terminal/*/MQL5') if path.is_dir())
    override = env.get('BRIDGE_BASE_PATH', '').strip()
    return {
        'platform': platform_name,
        'terminal_app_installed': app.is_dir() if platform_name == 'Darwin' else None,
        'wine_terminal_binary_found': (terminal_root / 'terminal64.exe').is_file() if platform_name == 'Darwin' else None,
        'metaeditor_binary_found': (terminal_root / 'metaeditor64.exe').is_file() if platform_name == 'Darwin' else None,
        'mql5_candidates': [str(path) for path in candidates],
        'active_terminal_data_path_verified': False,
        'bridge_override_configured': bool(override),
        'bridge_override_exists': Path(override).is_dir() if override else False,
        'native_mt5_binding_available': any(importlib.util.find_spec(name) is not None for name in ('MetaTrader5', 'mt5_wrapper')),
        'account_type_verified': False,
        'ea_compiled_and_attached_verified': False,
        'note': 'Filesystem candidates do not identify the active terminal data folder or prove account type. No broker connection was attempted.',
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    print(json.dumps(probe(), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
