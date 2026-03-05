import MetaTrader5 as mt5
import os
import time

def close_all():
    login = int(os.environ.get("MT5_LOGIN", 0))
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "")
    
    if not mt5.initialize(login=login, password=password, server=server):
        print("MT5 Init Failed")
        return

    positions = mt5.positions_get()
    if not positions:
        print("No open positions")
        mt5.shutdown()
        return

    print(f"Found {len(positions)} positions. Closing...")
    
    for p in positions:
        symbol = p.symbol
        volume = p.volume
        ticket = p.ticket
        order_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        
        tick = mt5.symbol_info_tick(symbol)
        price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": 0,
            "comment": "emergency close all",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"Failed to close {ticket}: {result.comment}")
        else:
            print(f"Closed {ticket} ({symbol})")
            
    mt5.shutdown()

if __name__ == "__main__":
    close_all()
