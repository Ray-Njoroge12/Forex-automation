# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FX AI Engine (SRS v1) — a deterministic, local-hosted Forex automation engine for proprietary trading on FX majors. Python intelligence + MT5 execution via a JSON-based file bridge. No LLMs, no cloud dependencies, no recurring costs.

All work must be validated against `/SRS_v1.md` (locked spec). The SRS defines non-negotiable risk parameters; never modify them without explicit user instruction.

## Commands

All commands run from `fx_ai_engine/`:

```bash
# Setup
pip install -r requirements.txt
python init_system.py              # Initialize dirs & SQLite DB

# Environment (required for live MT5)
export MT5_LOGIN=<login>
export MT5_PASSWORD=<password>
export MT5_SERVER=<server>

# Run
python main.py --mode smoke        # Single iteration (safe for testing)
python main.py --mode demo         # Continuous loop
python main.py --mode demo --iterations 100

# Tests
python -m pytest -q                # All tests
python -m pytest tests/test_agents.py -q  # Single test file

# Backtesting
python -m backtesting.bt_runner --csv tests/fixtures/ohlc_fixture.csv --symbol EURUSD

# MT5 connection validation
python test_bridge.py
```

**Key environment flags:**
- `USE_MT5_MOCK=1` — Use mock MT5 (required for testing without broker connection)
- `OTEL_ENABLED=1` — Enable OpenTelemetry tracing
- `PROM_ENABLED=1` / `PROM_PORT=8000` — Enable Prometheus metrics

## Architecture

### Decision Flow

```
M15 candle close
  → Regime Agent (H1 trend/volatility classification)
  → Technical Agent (M15 entry signal generation)
  → Adversarial Agent (spread/volatility/stacking challenge)
  → Portfolio Manager (position aggregation, max 2 trades, max 5% exposure)
  → Hard Risk Engine (independent veto — daily/weekly/drawdown stops)
  → Signal Router (atomic JSON write: tmp → rename → pending_signals/)
  → MT5 EA (MQL5 polls pending_signals/, places order)
  → Execution Feedback (MT5 writes result → bridge/feedback/)
  → SQLite (all decisions logged)
```

### Key Architectural Decisions

**Multi-agent isolation:** Each agent (`core/agents/`) is independently testable and produces typed outputs. The adversarial agent is specifically designed to reject signals — treat its rejections as correct behavior, not bugs.

**Hard Risk Engine is the sole authority** (`core/risk/hard_risk_engine.py`): It cannot be bypassed by any agent. It independently checks daily loss %, weekly loss %, drawdown %, and consecutive losses before any signal is routed.

**JSON bridge is the MT5 IPC boundary** (`bridge/`): Python writes signals as JSON to `bridge/pending_signals/`; the MQL5 EA (`mt5_ea/FX_Execution.mq5`) polls and consumes them. The `signal_router.py` uses atomic tmp→rename to prevent partial reads. `bridge/active_locks/` prevents duplicate signals for the same symbol.

**Mock-first testing:** `core/mt5_mock.py` mirrors the MT5 API surface. Set `USE_MT5_MOCK=1` to run the full stack without a broker connection. All tests use the mock.

### Component Map

| Path | Responsibility |
|------|---------------|
| `core/agents/` | Signal generation pipeline (regime → technical → adversarial → portfolio) |
| `core/indicators/calculators.py` | EMA(50,200), ATR(14), RSI(14) — deterministic, no side effects |
| `core/risk/hard_risk_engine.py` | Hard-locked risk constraints |
| `core/risk/exposure_manager.py` | Open position tracking |
| `core/mt5_bridge.py` | MT5 Python API wrapper |
| `bridge/signal_router.py` | Atomic JSON signal writing |
| `bridge/execution_feedback.py` | MT5 feedback ingestion |
| `database/db.py` | SQLite: trades, account_metrics, risk_events tables |
| `backtesting/bt_runner.py` | Backtrader orchestration entry point |
| `mt5_ea/FX_Execution.mq5` | MQL5 EA — only file that interacts with MT5 directly |

## Locked Constraints (SRS v1)

These values are not configurable — do not change without explicit instruction:

- Risk per trade: **3.2%**
- Max open trades: **2**
- Max combined exposure: **5%**
- Daily stop loss: **8%**
- Weekly stop loss: **15%**
- Drawdown halt: **20%**
- Consecutive loss halt: **3**
- Minimum R:R: **2.2**
- Instruments: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF
- Timeframes: H1 (regime), M15 (execution)

## Testing Notes

- Tests live in `fx_ai_engine/tests/` with fixtures in `tests/fixtures/`
- All 14 test files use `USE_MT5_MOCK=1` implicitly via mock — no broker needed
- The backtesting module uses `tests/fixtures/ohlc_fixture.csv` for unit tests
- `test_bridge.py` is an integration test requiring a live MT5 connection

## Pre-Live Validation Requirement

Per SRS v1, 30-day demo validation must pass before any live capital:
- ≥25 trades, ≥45% win rate, ≥2.0 avg R, ≤15% max drawdown
- Abort if: drawdown >20%, win rate <40%, avg R <1.8
