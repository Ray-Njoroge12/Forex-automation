# FX AI Engine — System Audit and Optimization Baseline

**Audit date:** 2026-08-27  
**Scope:** Current working tree, including Python engine, SQLite persistence, JSON/MT5 bridge, MQL5 EA, backtesting, and ML paths.  
**Verification:** `../.venv/bin/python -m pytest -q` — **226 passed**. The test suite gives regression coverage for much of the Python code but does not prove strategy profitability, live-data freshness, or MQL5 compilation/execution.

## Executive assessment

The project has a credible deterministic architecture and unusually good operational intent: audit trails, failure handling, recovery logic, trade-management persistence, a mock bridge, a forward-validation protocol, and ML kept out of the default live route. It is **not ready for optimization using reported backtest metrics or for live-capital use**.

The principal reason is an execution-control gap: Python approves each symbol against the same unmodified account snapshot, then the EA independently consumes every queued file. The two-trade/five-percent portfolio limits can therefore be exceeded within one decision cycle. The EA also enforces a default 5-pip spread ceiling while the core policy specifies 2 pips. These are safety-policy violations, not merely tuning issues.

The current strategy is a trend-following pullback system rather than a general AI system:

```
closed M15/H1/H4 data -> trend regime -> pullback + candle confirmation
  -> spread/volatility/exposure challenge -> sizing and hard risk gate
  -> JSON queue -> MT5 execution/trade management -> SQLite evidence
```

Its intelligence is deterministic, explainable, and mostly rule based. `ml/signal_ranker.py` and `ml/meta_labeler/` are research/shadow facilities; they do not establish a trained production decision model.

## Architecture and what is working well

| Area | Evidence | Assessment |
|---|---|---|
| Separation of concerns | Agents, risk engine, router, EA, state sync, and DB are distinct components. | Good base for hardening and targeted regression tests. |
| Explainability | Named reason codes and `decision_funnel_events` persist approvals/rejections. | Strong diagnostic capability. |
| Broker reconciliation | Snapshot/state reconciliation and management-state restore attempt to fail closed. | A meaningful safety design, especially around restarts. |
| Signal delivery | Python validates the payload and publishes through temp-file then rename. | Good local-file handoff pattern. |
| Risk-aware entry design | H1/H4 alignment, M15 pullback/candle confirmation, ATR/structure stop, R:R check, session/news gates. | Coherent trend-continuation hypothesis, but unproven. |
| Research controls | Non-SRS modes and experiments are explicitly configured; meta-labeler defaults to shadow/canary behavior. | Sensible containment of experimental logic. |

## Confirmed findings

Severity refers to capital/reliability impact if the system is used on a connected account.

### Critical

1. **Portfolio and hard-risk caps can be bypassed by same-cycle queueing.**

   `TradingEngine._decision_cycle` iterates all six symbols (`main.py:2024-2026`). Each `_evaluate_symbol` checks `AccountStatus`, but that status is not reserved or updated after a signal is routed (`main.py:1633-1669`, `1939-1950`). A zero-position snapshot can thus approve several symbols before the EA opens any of them. The EA processes queued files with no max-position, aggregate-risk, or correlation enforcement (`mt5_ea/FX_Execution.mq5:1019-1218`). This can violate the core policy’s 2-position and 5% exposure constraints (`config_microcapital.py:143-150`).

   **Required correction:** introduce an atomic portfolio reservation ledger before routing. Count both broker-open and `PENDING`/`EXECUTION_UNCERTAIN` trades in every allocation and hard-risk decision. The EA must independently reject when an authenticated/risk-versioned envelope is expired, over capacity, or cannot be reconciled.

2. **The final execution spread control is looser than policy.**

   The core policy has `MAX_SPREAD_PIPS = 2.0` (`config_microcapital.py:150`), but the EA input defaults to `MaxSpreadPips = 5.0` (`mt5_ea/FX_Execution.mq5:5-7`) and uses that setting at order time (`1100-1107`). A market move can therefore turn a Python-approved order into a 2–5 pip execution.

   **Required correction:** make the policy a single versioned artifact delivered in the signal, configure/verify the EA at startup, and enforce the stricter of EA and payload ceilings. Record both signal-time and fill-time spread in pips with an explicit reject reason.

3. **Market-calendar and macro inputs are stale with no fail-closed freshness check.**

   The calendar ends in June 2026 (`data/economic_calendar.json`); the rate file says last updated 2026-02-28 (`data/rate_differentials.json`). The loader accepts any readable JSON, and on read/parse failure disables the calendar filter (`core/filters/calendar_filter.py:42-55`). On the audit date this leaves the system trading through unknown high-impact events.

   **Required correction:** version data with source, retrieval time, expiry, checksum, and coverage horizon. Block new entries if required calendar data is absent, malformed, expired, or lacks the next 7–14 days. Treat rate differentials as stale after a defined period and disable only their soft modifier with an event, not silently.

### High

4. **Backtest is not a faithful production simulation and should not be used to claim edge.**

   In core SRS mode it explicitly labels itself “Smoke-test simulation,” starts with $10,000, uses a fixed 1.2-pip spread, zero slippage/commission, and bypasses realistic feasibility (`backtesting/simulation_profile.py:35-71`; `bt_strategy.py:240-245`). It does not invoke `HardRiskEngine`, the calendar gate, SQLite/queue behavior, execution rejection/retry behavior, loss-streak halts, or the live multi-symbol portfolio. The single-symbol strategy returns whenever one position exists (`bt_strategy.py:160-165`).

   **Required correction:** create one event-driven simulator shared with the live decision functions. Replay all six symbols chronologically; model bid/ask, variable spreads, spread/slippage/commission/swap, stop-level rules, pending orders, partial exits, EA restarts, stale snapshots, and queue delays. Apply the exact risk/reservation logic and report per-pair and portfolio results.

5. **Execution uses a different risk/cost model than research.**

   EA `CalculateLot` sizes only stop loss (`mt5_ea/FX_Execution.mq5:484-515`). The “realistic” simulator adds assumed spread, slippage, and $7/lot commission (`backtesting/simulation_profile.py:85-118`). Python feasibility mirrors the EA’s stop-only model (`core/mt5_bridge.py:944-1039`). This creates disagreement over intended loss and min-lot viability.

   **Required correction:** define one canonical broker-contract sizing calculation (ideally broker `OrderCalcProfit`/`OrderCalcMargin`) and use it in pre-route validation, EA, and simulator. Include worst-case allowed spread/slippage and validate actual loss at the final SL after fill.

6. **The strategy has parameter contradictions that widen its effective entry rule.**

   `TechnicalThresholds` defaults to a 2-pip pullback buffer and bounded RSI (`technical_agent.py:18-34`), but the active shared policy overrides them to a **20-pip** buffer and RSI 0–100 (`config_microcapital.py:111-125`). That effectively removes RSI qualification and substantially relaxes “near EMA” on most majors. No documented, out-of-sample evidence supports the active values.

   **Required correction:** inventory every live parameter with provenance, expected unit, intended default, and frozen validation version. Restore/choose values only after purged walk-forward evaluation and separate ablations for RSI, pullback width, structural stop, and management rules.

7. **EA order/management robustness lacks broker-rule validation and observability.**

   The EA calculates/normalizes SL/TP but does not prevalidate trade stop/freeze levels for initial market or limit orders (`mt5_ea/FX_Execution.mq5:1127-1162`). It assumes two decimal volume normalization (`514`, `287`, `537`), which is incorrect for brokers whose volume step needs more precision. It also reads/writes hand-built JSON using a simplistic parser (`385-415`) and ANSI file encoding (`417-442`).

   **Required correction:** validate order direction, symbol trade mode, price side, min stop/freeze distance, volume grid, and margin before send; normalize volume from `SYMBOL_VOLUME_STEP` precision. Use a robust, constrained message grammar or an MQL JSON library, escape fields, and emit the MT5 retcode plus requested/final SL/TP/volume in feedback.

### Medium

8. **Correlation control is statistically weak and can be misleading.**

   It calculates absolute Pearson correlation on only five D1 close levels (`portfolio_manager.py:51-52`, `111-132`), not returns, and strips the sign. Five price levels are insufficient and absolute correlation loses whether positions offset or compound risk. The static map is also incomplete.

   **Required correction:** use synchronized log returns, an adequately long rolling window (e.g. 60–120 observations), confidence/min-data rules, and direction-adjusted currency-factor exposure. Include pending reservations.

9. **Session logic is static UTC and does not represent DST or liquidity nuances.**

   London is hard-coded 07:00–16:00 and New York 13:00–21:00 UTC (`core/filters/session_filter.py:8-23`). These do not track daylight-saving shifts; spreads and liquidity change materially around overlaps, rollovers, holidays, and broker server time.

   **Required correction:** use named time zones and a trading-calendar model, add rollover/Friday/holiday rules, and verify session classification in unit tests around DST boundaries.

10. **Persistence can overwrite lifecycle evidence.**

    `insert_trade_proposal` uses `INSERT OR REPLACE` (`database/db.py:331-371`). In SQLite this is delete-and-reinsert, so a repeated proposal identifier can erase associated state rather than safely preserving an immutable event history.

    **Required correction:** make trade decisions append-only, use explicit idempotent inserts, retain immutable signal versions, and model state transitions separately with allowed transition checks.

11. **The documented and actual test baselines differ.**

    `docs/SYSTEM_ANALYSIS.md` claims 261 passing tests, whereas the current workspace runs 226. This does not indicate failure, but it makes readiness claims non-reproducible. MQL5 compilation and realistic end-to-end broker-contract tests are not demonstrated by the Python suite.

    **Required correction:** publish a CI matrix: Python unit/integration, SQLite migration, property tests for sizing/risk, MT5 mock contract tests, EA compile test, and controlled demo end-to-end tests.

## What has not been established

- No full historical multi-pair dataset is included, so expectancy, drawdown, market-regime robustness, and per-pair performance cannot be measured here.
- No reproducible out-of-sample or forward-demo results are present.
- No evidence demonstrates that broker symbol suffixes, account currency, commissions, swaps, contract values, or EA configuration match the intended account.
- The ML ranker is not production intelligence: its threshold defaults to 0.0 and the documented training prerequisite is 500+ closed trades. Its model artifacts and data lineage must be validated before it may influence routing.

## Optimization program requirements

### Phase 0 — Safety release (block optimization/live use until accepted)

1. Atomic risk reservations for all queued/open/uncertain trades.
2. EA policy parity, signed/versioned signal envelope, TTL, and account/portfolio recheck at execution.
3. Fail-closed freshness gates for calendar, broker quotes, snapshot, and policy/config version.
4. Canonical sizing and final-fill loss validation; hard reject any min-lot or cost-induced over-risk.
5. EA compile plus tests for spreads, trade limits, stale signals, restart recovery, malformed files, and rejected order retcodes.

**Acceptance criteria:** a synthetic cycle with six valid signals opens/routes at most two within the 5% envelope; a 2.01-pip fill is rejected in core SRS; an expired calendar blocks new entries; no expired/unknown-policy file reaches `OrderSend`.

### Phase 1 — Evidence-quality release

1. Obtain at least 3–5 years of broker-consistent M15 bid/ask data for each pair, including spreads and rollovers.
2. Build a multi-symbol event replay with a versioned dataset manifest and no look-ahead: decisions only use completed bars and orders execute at the next available bid/ask.
3. Add realistic and adverse cost scenarios, interrupted bridge/EA scenarios, and parameter-ablation reports.
4. Use purged, embargoed walk-forward partitions; hold out the most recent period untouched until parameters are frozen.

**Report every run:** trade count, expectancy/R, payoff ratio, median/95th execution cost, max drawdown, time-to-recovery, exposure breaches, profit factor, Sharpe/Sortino with caveats, per-symbol/per-regime outcomes, and sensitivity to costs and parameter changes.

### Phase 2 — Strategy optimization

Prioritize testing these hypotheses independently, not by broad grid search:

1. Correct pullback-buffer/RSI settings and their interaction with H1/H4 trend alignment.
2. ATR/structural stop distribution and the realized versus intended R after spread/cost.
3. Break-even, partial-close, and trail logic versus a fixed target; compare on the same signals.
4. Regime filters: ADX thresholds, trend-distance normalized by ATR, volatility state, and session.
5. Pair selection and currency-factor exposure rather than static pair correlation.

Parameters must be frozen before the SRS 30-day forward demo; do not train/enable ML until labels, time splits, leakage checks, calibration, and a shadow-to-canary promotion rule are complete.

### Phase 3 — Forward-demo release gate

Run the frozen system on a broker-matched demo account for at least 30 calendar days and 25 closed trades. The SRS thresholds (win rate >=45%, average R >=2.0, max drawdown <=15%) should be supplemented with zero hard-risk breaches, zero stale-data bypasses, zero unmatched execution/exit lifecycle events, and reconciliation success for every restart. Treat the trade-count threshold as a minimum sample, not proof of edge.

## Recommended immediate next actions

1. Implement and test the reservation/EA parity fixes before modifying strategy thresholds.
2. Replace or refresh the local calendar and rate files, then add expiry enforcement.
3. Build the shared multi-pair realistic replay and use its output to establish a new performance baseline.
4. Reconcile the active 20-pip / RSI-bypass parameters against their original intent and evidence.
5. Compile the EA in the target MT5 terminal and execute a controlled scenario suite on demo.

