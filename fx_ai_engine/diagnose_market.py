import os
import pandas as pd
from datetime import datetime, timezone
from core.credentials import load_mt5_credentials_from_env
from core.mt5_bridge import MT5Connection, mt5
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15

def diagnose():
    try:
        creds = load_mt5_credentials_from_env()
        bridge = MT5Connection(login=creds.login, password=creds.password, server=creds.server)
        if not bridge.connect():
            print("Failed to connect to MT5")
            return

        symbols = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"]
        print(f"\nDiagnostic Report - {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
        print("-" * 100)
        print(f"{'Symbol':<10} | {'Regime':<15} | {'Trend':<10} | {'M15 RSI':<8} | {'Pullback?':<10} | {'Verdict'}")
        print("-" * 100)

        for sym in symbols:
            regime_agent = RegimeAgent(sym, bridge.fetch_ohlc_data)
            regime = regime_agent.evaluate(TIMEFRAME_H1)
            
            # Technical evaluation
            tech_agent = TechnicalAgent(sym, bridge.fetch_ohlc_data, bridge.get_live_spread)
            
            # Fetch M15 for manual check in report
            m15_df = bridge.fetch_ohlc_data(sym, TIMEFRAME_M15, 50)
            if m15_df.empty:
                print(f"{sym:<10} | DATA ERROR")
                continue
            
            m15_df['ema'] = m15_df['close'].ewm(span=50, adjust=False).mean()
            from core.indicators import calculate_rsi
            m15_df['rsi'] = calculate_rsi(m15_df['close'], 14)
            
            curr = m15_df.iloc[-1]
            rsi_val = curr['rsi']
            
            # Pullback logic check
            ema_val = curr['ema']
            is_pulled = False
            if regime.trend_state == "BULLISH":
                is_pulled = curr['low'] <= ema_val
            elif regime.trend_state == "BEARISH":
                is_pulled = curr['high'] >= ema_val

            # Detailed verdict
            if regime.regime not in {"TRENDING_BULL", "TRENDING_BEAR"}:
                verdict = f"No Trend ({regime.regime})"
            elif not is_pulled:
                verdict = "Waiting for Pullback to EMA"
            elif rsi_val is None or pd.isna(rsi_val):
                verdict = "RSI Warming Up"
            elif regime.trend_state == "BULLISH" and not (40 <= rsi_val <= 65):
                verdict = f"RSI {rsi_val:.1f} Outside Buy Zone (40-65)"
            elif regime.trend_state == "BEARISH" and not (35 <= rsi_val <= 60):
                verdict = f"RSI {rsi_val:.1f} Outside Sell Zone (35-60)"
            else:
                verdict = "CONDITIONS MET - Check logs for filters (Spread/Macro)"

            print(f"{sym:<10} | {regime.regime:<15} | {regime.trend_state:<10} | {rsi_val:>7.1f} | {str(is_pulled):<10} | {verdict}")

        bridge.shutdown()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    diagnose()
