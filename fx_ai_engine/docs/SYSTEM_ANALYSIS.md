# FX AI Engine — Comprehensive System Analysis

**Date:** 2026-03-15
**Branch:** `claude/analyze-trading-system-Tqs4y`
**Test Results:** 261 passed, 1 skipped, 0 failed

---

## 1. Architecture Overview

The FX AI Engine follows a deterministic multi-agent pipeline for Forex signal generation with hard risk constraints. No LLMs, no cloud dependencies.

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

---

## 2. Signal Generation Pipeline

### 2.1 Regime Agent (`core/agents/regime_agent.py`)

**Role:** Classify the current market regime on H1 timeframe to provide context for entry decisions.

**Key Logic:**
- Uses EMA(50) vs EMA(200) for trend direction (bullish/bearish/neutral)
- ATR(14) for volatility classification (low/normal/high)
- ADX(14) for trend strength
- RSI(14) for overbought/oversold conditions
- Outputs: `RegimeOutput` with trend direction, volatility state, and confidence score

**Regime Classifications:**
- Trend: bullish, bearish, neutral
- Volatility: low, normal, high
- Combined regime drives downstream trade management parameters

### 2.2 Technical Agent (`core/agents/technical_agent.py`)

**Role:** Generate M15 entry signals with precise SL/TP levels.

**Key Logic:**
- EMA crossover signals (fast/slow)
- RSI momentum confirmation (RSI slope analysis)
- ATR-based stop loss calculation with structural snap
- Minimum R:R enforcement (>= 2.2)
- Structural SL snap: looks for nearby swing highs/lows within [0.8x, 1.5x] ATR window
- Regime-driven trade management params (BE trigger, partial close, trailing ATR mult)

**Output:** `TechnicalSignal` with direction, entry, SL, TP, and management parameters

### 2.3 Adversarial Agent (`core/agents/adversarial_agent.py`)

**Role:** Challenge and reject weak signals — its rejections are correct behavior, not bugs.

**Key Checks:**
- Spread filter: rejects if current spread exceeds threshold
- Volatility filter: rejects during abnormal volatility
- Stacking filter: prevents correlated pair exposure
- R:R re-verification
- Session timing validation

**Output:** `AdversarialDecision` with approved/rejected status and rejection reasons

### 2.4 Portfolio Manager (`core/agents/portfolio_manager.py`)

**Role:** Final pre-risk-engine allocator with exposure and stacking checks.

**Risk Modes:**
1. **Fixed-USD mode** (`FIXED_RISK_USD` env var): Converts fixed dollar amount to percent using live balance
2. **Percentage mode** (default SRS v1): Uses base_risk=3.2% scaled by ATR ratio and adversarial modifier

**Checks:**
- Max simultaneous trades: 2
- Max combined exposure: 5%
- Per-symbol position limits
- Optional ML signal ranker gate (disabled by default, threshold=0.0)

---

## 3. Risk Management

### 3.1 Hard Risk Engine (`core/risk/hard_risk_engine.py`)

**Sole authority — cannot be bypassed by any agent.**

| Constraint | Value | Implementation |
|-----------|-------|----------------|
| Risk per trade | 3.2% | `BASE_RISK_PCT: 0.032` |
| Max open trades | 2 | `MAX_SIMULTANEOUS_TRADES: 2` |
| Max combined exposure | 5% | `MAX_COMBINED_EXPOSURE: 0.05` |
| Daily stop loss | 8% | `DAILY_STOP_LOSS_PCT: 0.08` |
| Weekly stop loss | 15% | `WEEKLY_STOP_LOSS_PCT: 0.15` |
| Drawdown halt | 20% | `HARD_DRAWDOWN_PCT: 0.20` |
| Consecutive loss halt | 3 | `LOSS_HALT_THRESHOLD: 3` |
| Minimum R:R | 2.2 | `MIN_RISK_REWARD: 2.2` |

**Graduated Loss Streak Throttle:**
- 1 consecutive loss → 75% risk allocation
- 2 consecutive losses → 50% risk allocation
- 3+ consecutive losses → full trading halt

### 3.2 Exposure Manager (`core/risk/exposure_manager.py`)

- Tracks open positions per symbol
- Calculates total portfolio exposure
- Prevents duplicate symbol entries
- Feeds data to Hard Risk Engine for position-count checks

---

## 4. Execution Path

### 4.1 Signal Router (`bridge/signal_router.py`)

- Atomic JSON writing: writes to temp file, then `os.rename()` to `pending_signals/`
- Creates lock files in `active_locks/` to prevent duplicate signals per symbol
- Signal includes: symbol, direction, entry, SL, TP, lot size, trade management params

### 4.2 MT5 EA (`mt5_ea/FX_Execution.mq5`)

**Signal Consumption:**
- Polls `bridge/pending_signals/` every second
- Validates spread before order placement
- Calculates lot size respecting broker min/max/step limits
- Writes execution feedback to `bridge/feedback/execution_*.json`

**Trade Management (per-position):**
1. Break-Even: moves SL to entry + 1 pip buffer at configured R threshold
2. Partial Close: closes 50% at configured R multiple
3. Trailing Stop: ATR-based trailing after BE triggered
4. Parameters stored per-position in arrays, regime-driven

**Account Snapshots:**
- Writes `bridge/feedback/account_snapshot.json` every 5 seconds
- Contains: balance, equity, drawdown, open positions, margin

### 4.3 Execution Feedback (`bridge/execution_feedback.py`)

- Ingests MT5 execution results from `bridge/feedback/`
- Updates SQLite with fill prices, slippage, actual lot sizes
- Processes exit feedback from `bridge/exits/exit_*.json`
- Calculates final R-multiple and WIN/LOSS/BREAKEVEN status

---

## 5. Database Layer (`database/db.py`)

### Tables

| Table | Purpose |
|-------|---------|
| `trades` | All trade decisions (approved and rejected), P&L, R-multiple |
| `account_metrics` | Time-series: balance, equity, drawdown %, daily/weekly loss % |
| `risk_events` | Audit trail: rule triggers, severities, reasons |
| `decision_funnel_events` | Pipeline funnel tracking (which agent approved/rejected) |

### Evidence Partitioning
- `evidence_stream`: Combines policy_mode + execution_mode + account_scope
- Enables filtering by runtime context (e.g., `runtime_mt5_core_srs`)

---

## 6. Configuration & Policy Modes (`config_microcapital.py`)

### Policy Modes

| Mode | Status | Description |
|------|--------|-------------|
| `core_srs` | Production-ready | Locked SRS v1 constraints, the only live-capital mode |
| `preserve_10` | Research-only | $10 micro-capital preservation mode |
| `legacy_micro_capital` | Research-only | Legacy micro-capital path |

- Non-SRS policies require `FX_ALLOW_NON_SRS_POLICY=1` and `USE_MT5_MOCK=1`
- Policy approval is logged as a risk event with full audit trail

### Key Environment Variables
- `USE_MT5_MOCK=1` — Mock MT5 for testing
- `FIXED_RISK_USD` — Override percentage risk with fixed dollar amount
- `BRIDGE_BASE_PATH` — Override MT5 bridge directory
- `FX_ALLOW_NON_SRS_POLICY=1` — Allow non-SRS research modes

---

## 7. Backtesting (`backtesting/`)

### bt_runner.py
- Backtrader-based orchestration
- Accepts OHLC CSV data for historical simulation
- Applies the same signal pipeline logic as live trading

### bt_strategy.py
- Backtrader strategy implementing the multi-agent pipeline
- Regime classification on H1 bars, entry on M15
- Respects all SRS constraints during simulation
- Tracks per-trade R-multiple and drawdown

### walk_forward_suite.py
- Walk-forward validation: train on N months, test on M months
- Rolling window analysis for parameter stability
- Generates performance metrics per fold

---

## 8. ML Signal Ranker (`ml/signal_ranker.py`)

**Status:** Research-only, not yet trained (requires 500+ closed trades)

**Model:** XGBoost (300 estimators) or RandomForest fallback

**Features (11):** regime_confidence, rsi, atr_ratio, spread_pips, session flags, rate_differential, stop_pips, risk_reward, direction_buy, rsi_slope

**Integration:** Optional pre-routing gate in Portfolio Manager (SRS default threshold=0.0, effectively disabled)

---

## 9. Monitoring (`dashboard/app.py`)

Streamlit-based read-only dashboard:
- **Account Metrics:** Balance, equity, drawdown time-series
- **Trades:** Last 200 trades with full detail
- **Risk Events:** Audit trail of all constraint triggers
- Evidence stream filtering for multi-context analysis

---

## 10. Test Coverage

**261 tests passing across 31 test modules**

| Category | Tests | Status |
|----------|-------|--------|
| Agent pipeline | 18 | Pass |
| Risk engine | 9 | Pass |
| MT5 bridge contracts | 19 | Pass |
| Configuration & policy | 26 | Pass |
| Database helpers | 10 | Pass |
| Schemas & types | 11 | Pass |
| Integration pipeline | 6 | Pass |
| Backtesting | 5 | Pass |
| Startup gates | 16 | Pass |
| Validation criteria | 24 | Pass |
| All others | 117 | Pass |

### Previously Failing Tests (Fixed)
1. `test_is_demo_account_detects_demo_and_real_trade_modes` — `USE_MT5_MOCK` shortcut bypassed monkeypatched trade mode detection. Fixed by clearing env var in test.
2. `test_main_allows_non_srs_policy_in_mock_mode_with_explicit_approval` — `risk_events` table missing during test. Fixed by mocking `_insert_risk_event_with_context`.

---

## 11. SRS v1 Compliance

**All 10 locked constraints verified in code:**

| # | Constraint | SRS Value | Verified |
|---|-----------|-----------|----------|
| 1 | Risk per trade | 3.2% | Yes |
| 2 | Max open trades | 2 | Yes |
| 3 | Max combined exposure | 5% | Yes |
| 4 | Daily stop loss | 8% | Yes |
| 5 | Weekly stop loss | 15% | Yes |
| 6 | Drawdown halt | 20% | Yes |
| 7 | Consecutive loss halt | 3 | Yes |
| 8 | Minimum R:R | 2.2 | Yes |
| 9 | Instruments | 6 FX majors | Yes |
| 10 | Timeframes | H1/M15 | Yes |

---

## 12. Production Readiness Assessment

**Ready:**
- Core SRS policy mode is locked and fully tested
- Hard risk engine is independent authority with no bypass paths
- Full audit trail in SQLite
- Mock-first testing strategy
- 30-day demo validation infrastructure in place

**Pre-Live Requirements (per SRS v1):**
- 30-day demo validation must pass:
  - >= 25 trades
  - >= 45% win rate
  - >= 2.0 average R-multiple
  - <= 15% max drawdown
- Abort if: drawdown >20%, win rate <40%, avg R <1.8

**Minor Gaps (non-blocking):**
- Signal ranker not yet trained (needs 500+ live trades)
- Dashboard has no unit tests (acceptable for read-only Streamlit UI)
- 2 empty test modules (backtesting_profiles, walk_forward_reporting)
