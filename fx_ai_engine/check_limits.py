import os
from core.credentials import load_mt5_credentials_from_env
from core.mt5_bridge import MT5Connection, mt5

def get_broker_limits():
    try:
        creds = load_mt5_credentials_from_env()
        bridge = MT5Connection(login=creds.login, password=creds.password, server=creds.server)
        
        if not bridge.connect():
            print(f"Connect failed: {bridge.last_error}")
            return

        symbols = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"]
        print(f"{'Symbol':<10} | {'Min Lot':<8} | {'Lot Step':<8} | {'Max Lot':<8} | {'Tick Value':<10}")
        print("-" * 60)
        
        for sym in symbols:
            info = mt5.symbol_info(sym)
            if info:
                print(f"{sym:<10} | {info.volume_min:<8.2f} | {info.volume_step:<8.2f} | {info.volume_max:<8.2f} | {info.trade_tick_value:<10.2f}")
            else:
                print(f"{sym:<10} | NOT FOUND")
                
        bridge.shutdown()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    get_broker_limits()
