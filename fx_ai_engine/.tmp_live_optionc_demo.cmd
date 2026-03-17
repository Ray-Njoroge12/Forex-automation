@echo off
setlocal
cd /d C:\Users\rayng\Desktop\Forex-automation\fx_ai_engine
set PYTHONUNBUFFERED=1
set USE_MT5_MOCK=0
set FX_POLICY_MODE=core_srs
set FX_EXPERIMENT_AUDUSD_PULLBACK_RELAX=1
set FX_EXPERIMENT_LIVE_TRADE_MGMT_OPTION_C=1
set BRIDGE_BASE_PATH=C:\Users\rayng\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files\bridge
C:\Users\rayng\AppData\Local\Programs\Python\Python311\python.exe main.py --mode demo --iterations 720 --policy-mode core_srs

