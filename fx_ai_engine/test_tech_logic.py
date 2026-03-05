import os
import pandas as pd
from datetime import datetime, timezone
from core.credentials import load_mt5_credentials_from_env
from core.mt5_bridge import MT5Connection, mt5
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15

def test_tech():
    try:
        creds = load_mt5_credentials_from_env()
        bridge = MT5Connection(login=creds.login, password=creds.password, server=creds.server)
        if not bridge.connect():
            return

        sym = "USDCAD"
        regime_agent = RegimeAgent(sym, bridge.fetch_ohlc_data)
        regime = regime_agent.evaluate(TIMEFRAME_H1)
        
        tech_agent = TechnicalAgent(sym, bridge.fetch_ohlc_data, bridge.get_live_spread)
        
        # Manually trace evaluation
        print(f"Testing {sym}...")
        print(f"Regime: {regime.regime}")
        
        # This is where we trace the logic in technical_agent.py
        h1 = bridge.fetch_ohlc_data(sym, TIMEFRAME_H1, 350)
        m15 = bridge.fetch_ohlc_data(sym, TIMEFRAME_M15, 350)
        
        from core.indicators import calculate_ema, calculate_atr, calculate_rsi
        h1["ema_fast"] = calculate_ema(h1["close"], 50)
        h1["ema_slow"] = calculate_ema(h1["close"], 200)
        m15["ema_fast"] = calculate_ema(m15["close"], 50)
        m15["atr"] = calculate_atr(m15, 14)
        m15["rsi"] = calculate_rsi(m15["close"], 14)
        
        h1_last = h1.iloc[-1]
        m15_last = m15.iloc[-1]
        
        print(f"H1 EMA Fast: {h1_last['ema_fast']:.5f}, Slow: {h1_last['ema_slow']:.5f}")
        print(f"M15 EMA Fast: {m15_last['ema_fast']:.5f}, Last Low: {m15_last['low']:.5f}, RSI: {m15_last['rsi']:.1f}")
        
        pip_val = 0.0001
        buffer = 2.0 * pip_val
        
        if h1_last["ema_fast"] > h1_last["ema_slow"]:
            pulled_back = (m15_last["low"] <= m15_last["ema_fast"] + buffer)
            rsi_ok = 35 <= m15_last["rsi"] <= 70
            print(f"BULLISH: pulled_back={pulled_back}, rsi_ok={rsi_ok}")
            
            # Trace RR
            atr_multiplier = 1.2
            pip_value = 0.0001
            stop_pips = float((m15_last["atr"] * atr_multiplier) / pip_value)
            take_profit_pips = float(stop_pips * 2.2)
            live_spread = bridge.get_live_spread(sym)
            spread_pips = live_spread / pip_value
            effective_stop = stop_pips + spread_pips / 2
            effective_tp = take_profit_pips - spread_pips / 2
            rr = effective_tp / effective_stop
            print(f"Stop: {stop_pips:.1f}, TP: {take_profit_pips:.1f}, Spread: {spread_pips:.1f}, R:R: {rr:.2f}")
        
        signal = tech_agent.evaluate(regime, TIMEFRAME_M15, TIMEFRAME_H1)
        print(f"Final Signal: {signal}")

        bridge.shutdown()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_tech()
