import MetaTrader5 as mt5
from datetime import datetime
import os

def analyze_pnl():
    login = int(os.environ.get("MT5_LOGIN", 0))
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "")
    
    mt5.initialize(login=login, password=password, server=server)
    deals = mt5.history_deals_get(datetime(2025, 1, 1), datetime.now())
    
    if deals is None:
        print("No historical deals found")
        return
        
    pnl_deals = [d for d in deals if d.profit != 0.0]
    total_pnl = sum(d.profit for d in pnl_deals)
    
    print(f"Total MT5 PnL: ${total_pnl:.2f}")
    print("Recent Closed Trades:")
    
    for d in pnl_deals[-30:]:
        print(f"[{datetime.fromtimestamp(d.time)}] {d.symbol} | Vol: {d.volume} | PnL: ${d.profit:.2f} | Comment: {d.comment}")

    mt5.shutdown()

if __name__ == "__main__":
    analyze_pnl()
