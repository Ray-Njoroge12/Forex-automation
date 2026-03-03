# FX AI Engine (SRS v1)

Deterministic, local-hosted Forex automation engine aligned to `SRS_v1`.

## Scope
- Forex majors only
- Proprietary capital only
- Python intelligence + MT5 execution bridge
- No LLM/RL in v1

## Quickstart
1. Create environment and install dependencies:
   - `pip install -r requirements.txt`
2. Initialize project folders and DB:
   - `cd fx_ai_engine`
   - `python init_system.py`
3. Set MT5 credentials via environment variables:
   - `MT5_LOGIN`
   - `MT5_PASSWORD`
   - `MT5_SERVER`
4. Run bridge smoke test:
   - `python test_bridge.py`

## Tests
- `python -m pytest -q`

## Run Modes
- Smoke mode: `python main.py --mode smoke`
- Demo mode: `python main.py --mode demo`

## Backtesting (Backtrader)
1. Use an OHLC CSV with columns: `time,open,high,low,close[,volume]`
2. Run:
   - `python -m backtesting.bt_runner --csv tests/fixtures/ohlc_fixture.csv --symbol EURUSD`

## Observability (Local Only)
Environment variables:
- `USE_MT5_MOCK=1` (use mock MT5 for tests/dev)
- `OTEL_ENABLED=1` (enable OpenTelemetry console tracing)
- `OTEL_SERVICE_NAME=fx_ai_engine` (optional override)
- `PROM_ENABLED=1` (enable Prometheus metrics)
- `PROM_PORT=8000` (metrics port)

Metrics endpoint:
- `curl http://localhost:8000/metrics`
