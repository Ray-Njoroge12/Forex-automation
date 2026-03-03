# Automated FX AI Engine - System Requirements Specification (SRS) v1

## 1. Document Control
- Version: `v1.0`
- Date: `February 25, 2026`
- Status: `Approved for Implementation Planning`
- Project Type: `Proprietary Forex Automation (Zero-Cost Build)`

## 2. Purpose and Scope
This SRS defines the first production-ready specification for a proprietary, low-capital Forex automation engine designed for aggressive compounding under strict survival constraints.

This version is intentionally constrained for implementation simplicity, cost elimination, and behavioral reliability.

## 3. Locked Strategic Decisions
The following decisions are fixed for `v1` and must not be changed during build or 30-day forward validation:

1. Asset class: `Forex only`
2. Capital model: `Proprietary capital only`
3. Universe: `Majors only`
4. Infrastructure: `Local machine hosting only (no VPS in v1)`
5. Data source: `MetaTrader 5 broker feed only (no Alpha Vantage/Polygon for v1 execution logic)`
6. Intelligence model: `Deterministic Python multi-agent logic only`
7. LLM usage: `Disabled in v1`
8. RL execution layer: `Deferred (post-v1)`
9. Architecture: `Hybrid (Python intelligence + MT5 EA execution bridge)`

## 4. System Objectives
1. Generate structured, rule-based trade decisions on majors with controlled risk.
2. Preserve capital through hard, non-negotiable risk controls.
3. Maintain full auditability for every decision and execution event.
4. Validate live behavior on demo before any live capital deployment.

## 5. Trading Envelope

### 5.1 Instruments
- `EURUSD`
- `GBPUSD`
- `USDJPY`
- `AUDUSD`
- `USDCAD`
- `USDCHF`

### 5.2 Timeframes
- Regime/Bias confirmation: `H1`
- Execution logic: `M15`

### 5.3 Entry/Exit Policy
- Minimum `R:R` per trade: `>= 2.2`
- Stop basis: ATR/structure-driven (M15)
- No trade if spread filter fails.

## 6. Risk Policy (Hard-Locked for v1)
These limits override all agent outputs:

1. Risk per trade: `3.2%` (base)
2. Maximum simultaneous open trades: `2`
3. Maximum combined open exposure: `5%`
4. Daily stop loss: `8%`
5. Weekly stop loss: `15%`
6. Hard equity drawdown stop: `20%`
7. Consecutive-loss halt: `3 losses`

Additional safety rules:
- Lot sizing must be rounded down.
- If minimum executable lot violates allowed risk, trade must be rejected.
- No manual override of hard risk constraints.

## 7. High-Level Architecture

### 7.1 Components
1. Python Intelligence Core
2. Hard Risk Engine (independent authority)
3. JSON Execution Bridge
4. MT5 Execution EA (MQL5)
5. Account/Trade Feedback Sync
6. SQLite State and Audit Store

### 7.2 Topology (v1)
- Python core: local machine
- MT5 terminal + EA: local machine
- Communication: local atomic JSON files
- Database: local SQLite

## 8. Functional Requirements

### 8.1 Data Ingestion
1. System shall pull OHLC and symbol metadata from MT5 via `MetaTrader5` Python API.
2. System shall use broker-native quotes for spread and execution checks.
3. System shall not depend on external paid data APIs for v1 operation.

### 8.2 Indicator Layer
1. System shall implement deterministic versions of:
   - `EMA(50, 200)`
   - `ATR(14)`
   - `RSI(14)`
2. Indicator logic shall be transparent and testable.

### 8.3 Agent Layer
#### Regime Agent
1. Classify trend/range state from H1 context.
2. Output regime classification + confidence.

#### Technical Agent
1. Evaluate M15 entry logic under allowed regime only.
2. Produce stop/target proposal with `R:R >= 2.2`.
3. Return no signal if conditions are not met.

#### Adversarial Agent
1. Attempt to invalidate proposed trades.
2. Reject or risk-dampen based on spread, volatility distortion, session weakness, and stacking conditions.

#### Portfolio Manager
1. Aggregate validated signals.
2. Enforce max trades and total exposure constraints before risk engine.

### 8.4 Hard Risk Engine
1. Must execute independently from agent approval flow.
2. Must block any signal violating Section 6 limits.
3. Must support daily/weekly reset logic in UTC.
4. Must halt trading on drawdown/loss-streak triggers.

### 8.5 Execution Bridge
1. Python shall write approved signals as atomic JSON (`tmp file -> rename`).
2. Lock files shall prevent duplicate processing.
3. MT5 EA shall consume one signal at a time and delete upon processing.

### 8.6 MT5 Execution EA
1. Poll signal folder every second.
2. Recompute lot size internally before order send.
3. Validate spread and execution preconditions.
4. Place order and monitor SL/TP.
5. Write execution feedback to JSON for Python ingestion.

### 8.7 Account Status and Feedback
1. MT5 side shall continuously emit account snapshots (balance, equity, open risk, open USD exposure).
2. Python side shall parse snapshots and update runtime risk state.
3. Closed trade outcomes shall update loss streak, drawdown, and performance history.

### 8.8 Persistence and Logging
System shall persist all decisions/events in SQLite:
- Trade proposals
- Trade executions
- Rejections + reasons
- Risk-engine decisions
- Equity curve and drawdown stats
- Spread/slippage statistics

## 9. Data Model (v1 Minimum)

### 9.1 `trades`
- `id`
- `trade_id`
- `symbol`
- `direction`
- `entry_price`
- `stop_loss`
- `take_profit`
- `risk_percent`
- `status` (`PENDING|OPEN|CLOSED|REJECTED`)
- `profit_loss`
- `r_multiple`
- `regime`
- `spread_entry`
- `slippage`
- `open_time`
- `close_time`

### 9.2 `account_metrics`
- `id`
- `timestamp`
- `balance`
- `equity`
- `open_risk_percent`
- `open_usd_exposure_count`
- `daily_loss_percent`
- `weekly_loss_percent`
- `drawdown_percent`
- `consecutive_losses`
- `is_halted`

### 9.3 `risk_events`
- `id`
- `timestamp`
- `rule_name`
- `severity`
- `reason`
- `trade_id`

## 10. Non-Functional Requirements

### 10.1 Cost
- Must run at zero recurring infrastructure/API cost for v1 (excluding broker account and internet/electricity).

### 10.2 Reliability
- System must fail safe:
  - If Python fails: no new trades
  - If MT5 unavailable: trading paused
  - If state sync stale: signal rejection

### 10.3 Performance
- Designed for M15/H1 cadence (not HFT).
- End-to-end decision cycle shall complete well within candle interval.

### 10.4 Auditability
- Every approval/rejection must be timestamped with reason.
- Trade lifecycle must be reconstructable from DB logs.

### 10.5 Security (Local)
- Bridge folders shall be restricted to local process access.
- No plaintext storage of broker credentials in source files.

## 11. Development Sequence (Mandatory Order)
1. Project structure + SQLite schema
2. MT5 account snapshot writer + Python parser
3. Indicator/data ingestion layer
4. Regime, Technical, Adversarial, Portfolio agents
5. Hard Risk Engine + UTC reset logic
6. JSON bridge + MT5 EA execution module
7. End-to-end demo run and logging verification
8. 30-day forward demo validation

## 12. Validation Protocol (Before Live Capital)
No live deployment before successful 30-day demo.

### 12.1 No-Change Rule During Validation
- No parameter/risk changes
- No strategy tuning
- No discretionary override

### 12.2 Minimum Acceptance Criteria
1. Trades executed: `>= 25`
2. Win rate: `>= 45%`
3. Average R: `>= 2.0`
4. Max drawdown: `<= 15%`
5. Risk engine triggers verified
6. No critical execution anomalies

### 12.3 Abort Criteria
1. Drawdown `> 20%`
2. Win rate `< 40%`
3. Avg R `< 1.8`
4. Repeated spread/slippage control failures
5. Risk engine halt failures

## 13. Out-of-Scope (v1)
1. LLM-driven reasoning
2. Reinforcement-learning execution optimizer
3. Gold/indices trading
4. Multi-broker smart routing
5. Cloud/VPS deployment
6. Investor capital management features

## 14. Compliance and Operating Assumptions
1. System is for proprietary trading only in v1.
2. If expanded to third-party capital, licensing requirements must be re-assessed before deployment.
3. Tax reporting obligations remain with the account owner.

## 15. Change Control
Any change to these fields requires `SRS v1.x` update before implementation:
- Risk percentages and stops
- Instrument universe
- Timeframe model
- Execution bridge protocol
- Acceptance criteria

---

This `SRS v1` is the implementation baseline. Coding should start only against this locked document.
