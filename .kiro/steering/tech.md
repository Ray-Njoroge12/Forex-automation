# Technology Stack

## Language & Runtime

- Python 3.13+ (primary language)
- MQL5 (MetaTrader 5 Expert Advisor)

## Core Dependencies

### Trading & Market Data
- `MetaTrader5` / `MMetaTrader5` / `mt5-wrapper` - MT5 Python API (Windows only)
- `backtrader` - Backtesting framework

### Data & ML
- `pandas` - Data manipulation
- `numpy` - Numerical computing
- `scikit-learn` - ML signal ranking
- `joblib` - Model persistence
- `transformers` - Sentiment analysis (optional)
- `feedparser` - News feed parsing

### Observability
- `opentelemetry-api` / `opentelemetry-sdk` - Distributed tracing
- `prometheus-client` - Metrics collection

### UI & Utilities
- `streamlit` - Dashboard interface
- `psutil` - System monitoring

### Testing
- `pytest` - Test framework

## Database

- SQLite 3 - Local persistence for trades, account metrics, and risk events

## Build & Development Commands

### Environment Setup
```bash
pip install -r requirements.txt
```

### Initialize System
```bash
cd fx_ai_engine
python init_system.py
```

### Testing
```bash
# Run all tests
python -m pytest -q

# Run specific test file
python -m pytest tests/test_agents.py -v
```

### Running the Engine

Set MT5 credentials via environment variables:
```bash
set MT5_LOGIN=your_login
set MT5_PASSWORD=your_password
set MT5_SERVER=your_server
```

Run modes:
```bash
# Smoke test (single cycle)
python main.py --mode smoke

# Demo mode (continuous)
python main.py --mode demo

# Demo with iteration limit
python main.py --mode demo --iterations 100
```

### Backtesting
```bash
python -m backtesting.bt_runner --csv tests/fixtures/ohlc_fixture.csv --symbol EURUSD
```

### Utility Scripts
```bash
# Bridge smoke test
python test_bridge.py

# Check spread conditions
python check_spreads.py

# Review trade history
python review_trades.py

# Market diagnostics
python diagnose_market.py

# Analyze demo results
python analyze_demo_results.py

# Apply micro-capital configuration
python apply_microcapital_config.py

# View risk config for balance
python config_microcapital.py
```

## Environment Variables

### MT5 Configuration
- `MT5_LOGIN` - MT5 account login
- `MT5_PASSWORD` - MT5 account password
- `MT5_SERVER` - MT5 broker server

### Micro-Capital Mode ($10-$500 accounts)
- `MICRO_CAPITAL_MODE=1` - Enable micro-capital risk parameters
- `FIXED_RISK_USD=0.50` - Fixed USD risk per trade (overrides percentage)
- `MAX_SPREAD_PIPS=3.5` - Relaxed spread filter for demo/micro accounts
- `ML_PREDICT_THRESHOLD=-1.0` - ML ranker threshold (set to -1.0 to disable)

### Development & Testing
- `USE_MT5_MOCK=1` - Use mock MT5 for tests/dev
- `USE_SENTIMENT=1` - Enable sentiment analysis agent

### Observability
- `OTEL_ENABLED=1` - Enable OpenTelemetry console tracing
- `OTEL_SERVICE_NAME=fx_ai_engine` - Service name for tracing
- `PROM_ENABLED=1` - Enable Prometheus metrics
- `PROM_PORT=8000` - Metrics endpoint port

### Metrics Endpoint
```bash
curl http://localhost:8000/metrics
```

## Platform Requirements

- Operating System: Windows (required for MT5 integration)
- MetaTrader 5 terminal installed and configured
- Python 3.13 or higher
