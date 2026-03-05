# Project Structure

## Root Layout

```
fx_ai_engine/           # Main application package
├── core/               # Core business logic and agents
├── bridge/             # MT5 execution bridge (JSON-based)
├── database/           # SQLite schema and persistence layer
├── backtesting/        # Backtrader integration
├── ml/                 # ML signal ranking
├── validation/         # Demo validation utilities
├── dashboard/          # Streamlit dashboard
├── mt5_ea/             # MQL5 Expert Advisor
├── tests/              # Test suite
├── data/               # Static data files (calendar, rates)
├── docs/               # Documentation
└── setup/              # Windows Task Scheduler configs
```

## Core Module Organization

### `core/` - Business Logic
- `agents/` - Multi-agent decision system
  - `regime_agent.py` - Trend/range classification (H1)
  - `technical_agent.py` - Entry logic and R:R calculation (M15)
  - `adversarial_agent.py` - Signal invalidation and risk dampening
  - `portfolio_manager.py` - Position aggregation and exposure control
- `risk/` - Risk management
  - `hard_risk_engine.py` - Non-negotiable risk limits
  - `exposure_manager.py` - Position exposure tracking
  - `reset_scheduler.py` - Daily/weekly loss reset logic
- `filters/` - Pre-trade filters
  - `calendar_filter.py` - Economic news blackout windows
  - `session_filter.py` - London/NY session validation
  - `macro_filter.py` - Rate differential loading
- `indicators/` - Technical indicators
  - `calculators.py` - EMA, ATR, RSI implementations
- `sentiment/` - Sentiment analysis (optional)
  - `sentiment_agent.py` - News sentiment scoring
- `mt5_bridge.py` - MT5 Python API wrapper
- `mt5_mock.py` - Mock MT5 for testing
- `schemas.py` - Payload validation
- `types.py` - Type definitions
- `timeframes.py` - Timeframe constants
- `credentials.py` - Environment credential loading
- `logging_utils.py` - Logging configuration
- `observability.py` - OpenTelemetry tracing
- `metrics.py` - Prometheus metrics
- `state_sync.py` - Account state synchronization
- `bridge_utils.py` - Bridge path detection

### `bridge/` - Execution Bridge
- `pending_signals/` - Outbound signals to MT5 (JSON files)
- `active_locks/` - Lock files to prevent duplicate processing
- `feedback/` - Execution feedback from MT5 (JSON files)
- `exits/` - Trade exit notifications from MT5 (JSON files)
- `signal_router.py` - Signal file writer with atomic operations
- `execution_feedback.py` - Feedback file reader and parser

### `database/` - Persistence
- `schema.sql` - SQLite schema definition
- `db.py` - Database operations and migrations
- `trading_state.db` - SQLite database file

### `backtesting/` - Backtesting Framework
- `bt_strategy.py` - Backtrader strategy implementation
- `bt_runner.py` - Backtest execution script
- `data_loader.py` - OHLC data loading
- `walk_forward.py` - Walk-forward optimization

### `ml/` - Machine Learning
- `signal_ranker.py` - ML-based signal probability scoring

### `tests/` - Test Suite
- `fixtures/` - Test data files
- `test_*.py` - Unit and integration tests

## Key Files

- `main.py` - Main engine entry point
- `init_system.py` - System initialization script
- `requirements.txt` - Python dependencies
- `README.md` - Project documentation
- `SRS_v1.md` - System Requirements Specification (root level)

## Data Files

- `data/economic_calendar.json` - High-impact economic events
- `data/rate_differentials.json` - Interest rate differentials by pair

## MT5 Integration

- `mt5_ea/FX_Execution.mq5` - Expert Advisor for order execution
- Bridge communication via atomic JSON file operations
- Lock files prevent race conditions
- Feedback loop for execution confirmation and trade exits

## Naming Conventions

### Python Modules
- Snake case for files: `hard_risk_engine.py`
- Classes: PascalCase (`HardRiskEngine`)
- Functions/variables: snake_case (`validate_signal`)
- Constants: UPPER_SNAKE_CASE (`TIMEFRAME_M15`)

### Database Tables
- Snake case: `trades`, `account_metrics`, `risk_events`
- Columns: snake_case (`trade_id`, `profit_loss`)

### Bridge Files
- Signal files: `{trade_id}.json` (e.g., `EURUSD_20260304_123456.json`)
- Lock files: `{trade_id}.lock`
- Atomic writes: temp file → rename pattern

## Configuration Files

- `.env` - Environment variables (not in version control)
- `setup/windows_task_*.xml` - Windows Task Scheduler templates

## Testing Structure

- Unit tests co-located with source when appropriate
- Integration tests in `tests/` directory
- Fixtures in `tests/fixtures/`
- Mock MT5 available via `USE_MT5_MOCK=1`

## Architecture Patterns

### Multi-Agent Pipeline
1. Regime Agent → classify market state
2. Technical Agent → generate entry signal
3. Adversarial Agent → attempt invalidation
4. Portfolio Manager → aggregate and enforce exposure limits
5. Hard Risk Engine → final validation (independent authority)
6. Signal Router → atomic JSON write to bridge
7. MT5 EA → execution and feedback

### State Synchronization
- MT5 EA writes account snapshots to `feedback/account_snapshot.json`
- Python engine reads snapshots and updates `AccountStatus`
- Execution feedback consumed and persisted to SQLite
- Daily/weekly resets managed by `ResetScheduler`

### Error Handling
- Fail-safe design: if Python fails, no new trades
- Stale state detection halts trading
- All rejections logged with reason codes
- Risk events tracked in dedicated table
