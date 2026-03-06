from __future__ import annotations

import sqlite3

import pandas as pd

from database.db import DB_PATH


def main() -> int:
    try:
        conn = sqlite3.connect(DB_PATH)

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
        print(df.to_string(index=False))

        profit_query = """
        SELECT
            COALESCE(SUM(profit_loss), 0.0) AS total_profit,
            COUNT(trade_id) AS total_trades,
            SUM(CASE WHEN profit_loss > 0 THEN 1 ELSE 0 END) AS winning_trades
        FROM trades
        WHERE status LIKE 'CLOSED%'
        """
        df_profit = pd.read_sql_query(profit_query, conn)
        print("\nProfitability Summary:")
        print(df_profit.to_string(index=False))
    except Exception as exc:
        print("Failed:", exc)
        return 1
    finally:
        if "conn" in locals() and conn:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
