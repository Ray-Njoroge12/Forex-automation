# Live Trade Management Option C Implementation Plan

## 1. Overview
- Implement an incremental **Option C** on top of the current trade-management path that already exists in `technical_agent.py`, `core/schemas.py`, and `mt5_ea/FX_Execution.mq5`.
- Goals:
  1. Keep a broker-side hard TP **and** allow trailing management on the same trade.
  2. Move to earlier protection thresholds for break-even / partial close.
  3. Persist per-position EA management state so MT5/EA restarts do not lose trade-management context.
- Success criteria:
  - Trending signals can emit `tp_mode="HYBRID"` behind a demo-only experiment flag.
  - The EA places a normal TP for HYBRID trades and still performs BE / partial / ATR trailing.
  - After an EA restart, open positions resume with the same management state and do not re-fire partial-close logic.
  - Python halts new trading if snapshot data shows open positions but management-state restore failed.
- Out of scope:
  - Changing SRS v1 constants (`3.2%` risk, `2` max trades, `5%` max exposure, `8%/15%/20%` stops, `3` consecutive losses, `2.2` min R:R).
  - Reworking hard-risk, router, or database schema unless audit gaps force a follow-up.

## 2. Prerequisites
- No new dependencies or package installs.
- No SQLite migration is required for the core Option C rollout if persistence is kept in the MT5 bridge feedback path.
- Add a demo-only runtime experiment in `fx_ai_engine/config_microcapital.py` (for example `LIVE_TRADE_MGMT_OPTION_C`) so rollout uses the existing `apply_runtime_experiment_config()` path and `main.py` demo-account gating.
- MT5 / MetaEditor access is required to compile and manually restart-test `fx_ai_engine/mt5_ea/FX_Execution.mq5`.
- Bridge feedback directory must remain writable because persistence files will live under `bridge/feedback/`.

## 3. Implementation Steps
### Step 1: Add Option C rollout control and Python-side mapping
- **Files:** `fx_ai_engine/config_microcapital.py`, `fx_ai_engine/core/agents/technical_agent.py`, `fx_ai_engine/tests/test_agents.py`
- Add a new experiment config read pattern in `TechnicalAgent.__init__()` similar to the existing experiment helpers.
- Update `TechnicalAgent._get_trade_management_params()` so:
  - experiment **off** -> current behavior remains unchanged,
  - experiment **on** and trending -> return `tp_mode="HYBRID"` plus earlier thresholds.
- Suggested starting values for the demo rollout:
  - LOW/NORMAL volatility: `be_trigger_r=0.5`, `partial_close_r=1.0`, `trailing_atr_mult=1.5`, `tp_mode="HYBRID"`
  - HIGH volatility: `be_trigger_r=0.75`, `partial_close_r=1.25`, `trailing_atr_mult=2.0`, `tp_mode="HYBRID"`
  - non-trending: keep current fixed-target behavior.
- Keep `TechnicalAgent.evaluate()` responsibility unchanged for lot/risk sizing; it must still output `take_profit_pips` at or above the existing `MIN_RISK_REWARD` guard.
- **Testing:** update the existing `test_trade_params_*` cases and add an experiment-off regression that proves old TRAIL/FIXED mapping still holds when Option C is disabled.

### Step 2: Extend signal schema compatibility for HYBRID mode
- **Files:** `fx_ai_engine/core/schemas.py`, `fx_ai_engine/tests/test_schemas.py`
- Update `validate_signal_payload()` to accept `tp_mode` values `FIXED`, `TRAIL`, and `HYBRID`.
- `technical_signal_to_payload()` can keep the current shape; it already serializes `tp_mode`, `be_trigger_r`, `partial_close_r`, and `trailing_atr_mult`.
- **Testing:** replace/add schema tests so one valid payload uses `tp_mode="HYBRID"`, and invalid strings still fail.

### Step 3: Teach the EA to manage HYBRID trades correctly
- **Files:** `fx_ai_engine/mt5_ea/FX_Execution.mq5`
- Update `ProcessPendingSignal()` so `tp_mode="HYBRID"` means:
  - place the normal broker TP using `take_profit_pips`,
  - also enable trailing logic for that position.
- Replace the current boolean-only `g_tp_mode_trail` model with richer per-position state, e.g. `g_trailing_enabled`, `g_keep_hard_tp` or a small mode enum.
- Update `ManageOpenPositions()` so trailing can run for HYBRID trades without zeroing the hard TP.
- **Critical fix:** stop computing `currentR` from `currentSL`; persist and use the **original risk distance / original SL** captured at fill time. Otherwise BE/trailing shrink the denominator and distort all later thresholds.
- **Testing:** manual compile in MetaEditor plus targeted Python-side documentation/tests that assert HYBRID payloads are emitted.

### Step 4: Persist and restore EA per-position management state
- **Files:** `fx_ai_engine/mt5_ea/FX_Execution.mq5`
- Add helper functions such as `SavePositionState(...)`, `LoadPersistedPositionStates()`, `DeletePositionState(...)`, and `ReconcilePersistedStateWithBroker()`.
- Persist one file per open position under `bridge/feedback/management_state_<position_ticket>.json` to simplify cleanup and restart recovery.
- State payload should include at least: `trade_id`, `position_ticket`, `symbol`, management mode, BE/partial/trailing parameters, original stop / original risk distance, `partial_closed`, and last update time.
- Call restore logic from `OnInit()`, persist on successful execution in `ProcessPendingSignal()`, update after partial closes / stop moves in `ManageOpenPositions()`, and delete on final close in `OnTradeTransaction()`.
- On startup, restore only files that still correspond to broker-open positions; quarantine or delete stale files for already-closed tickets.
- **Testing:** manual MT5 restart test with an already-open demo trade; verify no duplicate partial close and management resumes immediately.

### Step 5: Surface restore health through the existing feedback path
- **Files:** `fx_ai_engine/mt5_ea/FX_Execution.mq5`, `fx_ai_engine/main.py`, `fx_ai_engine/tests/test_main_reconciliation.py`, optionally `fx_ai_engine/tests/test_execution_feedback.py`
- Extend `WriteAccountSnapshot()` to include extra optional fields such as `management_state_restored`, `managed_positions_count`, and optionally `managed_position_tickets`.
- Reuse the existing `bridge/feedback/account_snapshot.json` path; `ExecutionFeedbackReader.read_account_snapshot()` already passes through optional fields after validation, so a code change in `bridge/execution_feedback.py` may not be necessary.
- Update `Engine._update_account_state()` to fail closed when live `open_positions_count > 0` but the EA reports restore failure or count mismatch between broker-open and managed positions.
- Record this as a risk/reconciliation event using the existing `STATE_RECONCILIATION_FAILED` / risk-event flow instead of inventing a bypass path.
- **Testing:** add a reconciliation test where snapshot extras indicate restore failure and assert `account_status.is_trading_halted` becomes `True`.

### Step 6: Roll out in the safest order
- Ship in this order:
  1. schema + tests,
  2. EA HYBRID compatibility with experiment still off,
  3. EA persistence/restore,
  4. Python fail-closed snapshot check,
  5. enable Option C only in demo mode via experiment,
  6. review demo behavior before making it the default.
- This order ensures old `FIXED` / `TRAIL` signals remain valid while the new EA logic lands first.

## 4. File Changes Summary
- **Modified:**
  - `fx_ai_engine/config_microcapital.py`
  - `fx_ai_engine/core/agents/technical_agent.py`
  - `fx_ai_engine/core/schemas.py`
  - `fx_ai_engine/mt5_ea/FX_Execution.mq5`
  - `fx_ai_engine/main.py`
  - `fx_ai_engine/tests/test_agents.py`
  - `fx_ai_engine/tests/test_schemas.py`
  - `fx_ai_engine/tests/test_main_reconciliation.py`
  - `fx_ai_engine/tests/test_execution_feedback.py` (only if a regression test for snapshot passthrough is added)
- **Created at runtime, not in git:** `bridge/feedback/management_state_<position_ticket>.json`
- **Deleted:** none in the repo; stale runtime state files should be removed by the EA when positions close.

## 5. Testing Strategy
- **Unit tests:**
  - `tests/test_agents.py`: HYBRID mapping, earlier thresholds, experiment-off fallback.
  - `tests/test_schemas.py`: `HYBRID` accepted; invalid `tp_mode` still rejected.
  - `tests/test_main_reconciliation.py`: engine halts on restore mismatch.
  - `tests/test_execution_feedback.py`: snapshot extras survive validation/read path.
- **Manual MT5 tests:**
  1. Compile `FX_Execution.mq5`.
  2. Route one demo trade with Option C enabled and confirm broker TP is present.
  3. Let BE / partial / trailing activate.
  4. Restart MT5/EA while the trade is still open.
  5. Confirm state reloads, no second partial close occurs, and trailing resumes.
- **Smoke validation:** run `USE_MT5_MOCK=1 python main.py --mode smoke` for Python-side regressions, then a short demo run for the live EA path.

## 6. Rollback Plan
- Disable the new experiment in config/env first; that returns Python emission to current `FIXED` / `TRAIL` behavior.
- Re-deploy the previous EA if needed.
- Only clean up `management_state_*.json` after affected positions are flat or manually reconciled.
- If restore mismatch is detected in production/demo, keep Python halted, rely on broker-side SL/TP already on the position, and resolve by either restoring state or manually closing the trade.

## 7. Estimated Effort
- **Effort:** ~1.5 to 2.5 developer days.
- **Complexity:** medium-high.
- **Main risks to watch under SRS v1:**
  - accidentally removing the hard TP for HYBRID trades,
  - recomputing R from the moved SL instead of the original risk,
  - duplicate partial closes after restart,
  - stale state files attaching to the wrong live position,
  - any change that weakens fail-safe behavior when MT5 state and Python state disagree,
  - any regression that touches locked SRS values or reduces the hard-entry `2.2` minimum R:R.
