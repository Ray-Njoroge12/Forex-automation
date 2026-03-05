import sqlite3
import pandas as pd

try:
    conn = sqlite3.connect('c:/Users/rayng/Desktop/Forex-automation/fx_ai_engine/database/trading_state.db')
    
    # Query MT5 trade history
    query = """
    SELECT 
        id, trade_id, symbol, status, reason_code, risk_percent, 
        spread_entry, profit_loss, slippage, r_multiple, open_time, close_time
    FROM trades
    ORDER BY id DESC
    LIMIT 100
    """
    df = pd.read_sql_query(query, conn)
    print("Recent Trade Proposals and Executions:")
    print(df.to_string())
    
    # Query profitability
    profit_query = """
    SELECT sum(profit_loss) as total_profit, count(trade_id) as total_trades, sum(case when profit_loss > 0 then 1 else 0 end) as winning_trades 
    FROM trades
    WHERE status IN ('EXECUTED', 'CLOSED') or profit_loss is not null
    """
    df_profit = pd.read_sql_query(profit_query, conn)
    print("\nProfitability Summary:")
    print(df_profit.to_string())

except Exception as e:
    print('Failed:', e)
finally:
    if 'conn' in locals() and conn:
        conn.close()
