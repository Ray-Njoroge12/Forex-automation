import MetaTrader5 as mt5
import os
from core.credentials import load_mt5_credentials_from_env

def check_spreads():
    creds = load_mt5_credentials_from_env()
    if not mt5.initialize(login=int(creds.login), password=creds.password, server=creds.server):
        print("Failed to initialize MT5")
        return
    
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

if __name__ == "__main__":
    check_spreads()
