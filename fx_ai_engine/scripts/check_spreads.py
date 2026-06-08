import MetaTrader5 as mt5
import os
import sys
from core.credentials import load_mt5_credentials_from_env


def check_spreads() -> int:
    creds = load_mt5_credentials_from_env()

    timeout_raw = os.getenv("MT5_INIT_TIMEOUT_MS", "15000").strip()
    try:
        timeout_ms = max(1000, int(timeout_raw))
    except ValueError:
        timeout_ms = 15000

    terminal_path = os.getenv("MT5_TERMINAL_PATH", "").strip()

    init_kwargs = {
        "login": int(creds.login),
        "password": creds.password,
        "server": creds.server,
        "timeout": timeout_ms,
    }
    if terminal_path:
        init_kwargs["path"] = terminal_path

    try:
        init_ok = mt5.initialize(**init_kwargs)
    except TypeError:
        # Compatibility for wrappers not exposing timeout/path kwargs.
        init_kwargs.pop("timeout", None)
        init_kwargs.pop("path", None)
        init_ok = mt5.initialize(**init_kwargs)

    if not init_ok:
        print("Failed to initialize MT5", mt5.last_error())
        return 1
    
    symbols = ['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF']
    for sym in symbols:
        info = mt5.symbol_info(sym)
        if info is None:
            print(f"{sym}: Symbol not found")
            continue
        pip = 0.0001 if 'JPY' not in sym else 0.01
        spread_pips = info.spread * info.point / pip
        print(f"{sym}: {spread_pips:.2f} pips")
    
    mt5.shutdown()
    return 0

if __name__ == "__main__":
    sys.exit(check_spreads())
