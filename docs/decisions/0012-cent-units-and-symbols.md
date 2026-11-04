# ADR 0012: Cent-account money units and broker symbol suffixes on the file bridge
Date: 2026-11-04 (amended 2026-11-04 after the second-model review, findings BR-001 to BR-006, branch `cent/integration-fixes`). Status: accepted. Branch `cent/bridge-track` (base `71bb25f`). `bridge/**` is a protected path: second-model review and owner approval are required before merge (the owner approved building on 2026-11-04).
## Context
`docs/STATUS.md` issue 27 and `fx_ai_engine/docs/runbooks/cent-account-setup.md` section 0 record what failed on a temporary USC copy of the bridge:
- A cent account reports balance, equity, margin, profit and tick value in cents (currency `USC`, 1,000 USC = $10). The file bridge passed the snapshot through in cents, while `evaluate_trade_feasibility` and `evaluate_fixed_risk_eligibility` converted the balance to dollars and read `tick_value` in cents. A 25-pip, 1.5% trade on $10 got `REJECTED_LOT_PREROUTE` with estimated lot 0.0006 instead of 0.06.
- Brokers such as Exness Standard Cent suffix symbols (`EURUSDc`). The EA and the engine hard-code the six plain names.
- `preserve_10` cannot start on the file bridge (`FILE_BRIDGE_PRESERVE_10_UNSUPPORTED`).
## Decision
### 1. One money-unit boundary: ingest
Every money value that leaves the bridge layer is US dollars. Lots stay broker lots. Prices, pips and ratios are never scaled. The scale is `account_unit_scale(currency)` from `_ACCOUNT_DENOMINATION_MAP` in `core/mt5_bridge.py` (USD 1.0; USC, USCENT, USDCENT, CENTUSD 0.01; any other currency 1.0, as before, with no new error). Conversion is `usd_from_account_units(value, scale)`: `round(value * scale, 8)` when the scale is not 1.0, the identity otherwise, so USD output is byte for byte unchanged. A currency that is missing or empty is not scale 1.0: it is unknown, and no money value is produced from it (section 7). When the scale is not 1.0 a snapshot dict gains `money_unit: "USD"` and `unit_scale`; for USD these keys are absent. `currency` stays as the broker reported it.
| Raw source | Converted in | Fields |
| --- | --- | --- |
| `MT5Connection.get_account_snapshot` | `core/mt5_bridge.py` | balance, equity, margin_free, floating_pnl |
| `FileBridgeConnection.get_account_snapshot` | `core/file_bridge_connection.py` | same four |
| `ExecutionFeedbackReader.read_account_snapshot` (feeds `update_account_state`) | `bridge/execution_feedback.py` | same four |
| exit_*.json and execution_*.json (`collect_*`, `consume_*` and the non-deleting `read_execution_feedback`) | `bridge/execution_feedback.py` | `profit_loss`, scale from the sibling `account_snapshot.json` currency (last good value cached; unknown until a readable snapshot names a currency, and then nothing is returned or deleted) |
| `get_symbol_execution_contract` (both connections) | `core/mt5_bridge.py`, `core/file_bridge_connection.py` | `tick_value`. This fixes both `evaluate_*` checks without changing their math |
| Deal summaries (both connections) | `core/mt5_bridge.py` `_closed_history_summary_from_deals(unit_scale=)` | per-leg and total `profit_loss`; no summary while the currency is unknown |
| Offline copies of bridge files | `research/demo_records.py` loaders | deal money fields, exit and execution `profit_loss`, deal volume in standard-lot equivalents (cent contract size 1,000 vs 100,000); currency from `--account-currency` or `status.json`, never assumed |
Consumer audit. No change needed, because they now receive dollars: `core/state_sync.py` (ratio based), `core/account_status.py`, `database/**`, `dashboard/app.py`, `application/market_data_service.py`, `application/feedback_service.py`, `application/engine_runtime.py`, `main.py`. A USC engine test (`tests/test_cent_units.py::test_usc_engine_end_to_end`) shows trade `profit_loss` and `account_status.balance` in dollars. `validation/reconciliation_inventory.py` has no money fields. `research/synthetic_demo.py`, `research/cost_stress.py` and `backtesting/**` are USD or synthetic and untouched.
### 2. Documented exception: Preserve-10 facts
`Preserve10AccountFacts` and `Preserve10SymbolFacts` keep reported (raw) values next to `unit_scale` and `normalized_*_usd`. `main.py` multiplies `unit_scale * tick_value` and `unit_scale * min_lot_margin` itself, so converting these dataclasses would scale twice. `_account_info()` on both connections stays the private raw broker view.
### 3. Preserve-10 on the file bridge stays unsupported
`FILE_BRIDGE_PRESERVE_10_UNSUPPORTED` stays. The approval facts need `leverage`, `trade_allowed`, a quote timestamp and `min_lot_margin` (MT5 `order_calc_margin`) for six symbols. The EA exports no leverage and no margin (`FX_Execution.mq5` has no `ACCOUNT_LEVERAGE` or `OrderCalcMargin`), and `market_quotes.json` has no per-symbol margin. Computing margin in Python from a guessed leverage would put an approximation inside a fail-closed approval gate. Adding exports belongs to the EA track. The cent path uses the micro percent policy. A test pins the unsupported result on USD and USC bridges. To enable it later the EA would need to export `leverage` and per-symbol margin for the minimum lot.
### 4. Symbol suffix and EA profile validation (C1 Python half)
`status.json` fields are optional. A bridge without them (old EA) behaves exactly as before. When present they are validated in `FileBridgeConnection._status()`:
| Field | Rule | Failure code |
| --- | --- | --- |
| `symbol_suffix` | string (may be empty) | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| `symbols_ok` | boolean | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| `symbol_errors` | list of strings; non-empty while `symbols_ok` is true is invalid | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| `symbols_ok` is false, or `symbol_errors` is non-empty while `symbols_ok` is absent | every gated read fails closed; message lists `symbol_errors` (or `unspecified`) | `FILE_BRIDGE_SYMBOLS_NOT_OK` |
| `account_currency` | non-empty string | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| `max_spread_pips_input` | finite number >= 0, not a bool | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| `live_guard` | object | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| `account_margin_mode` | string or int (shape only) | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| status vs snapshot currency | both present and different (case-insensitive) | `FILE_BRIDGE_ACCOUNT_CURRENCY_MISMATCH` |
| `symbols.json` `broker_symbol` | non-empty string when present | `FILE_BRIDGE_STATUS_FIELD_INVALID` (wrapped by `FILE_BRIDGE_SYMBOL_SPEC_UNAVAILABLE`, precise code kept) |
`FileBridgeDataError` now carries an optional `reason_code`; `_code_for(exc, default)` keeps the precise code when the error is turned into a `BridgeError`. The Python side keeps canonical names (`EURUSD`) everywhere; file names and bar payload symbols stay canonical, and the EA owns the mapping to the broker symbol. `FileBridgeConnection.get_ea_profile()` returns the validated fields (None when absent) without the `symbols_ok` gate, for `validation.file_bridge_check` and for Policy-Live.
**Which readers the `symbols_ok` gate covers.** The gate lives in `FileBridgeConnection._status()` (`_read_status(require_symbols_ok=True)`). Gated, because they feed a decision or an order: `connect`, `server_utc_offset_seconds`, `get_account_snapshot` and `_account_info` (and so `is_demo_account`, `evaluate_trade_feasibility`, `evaluate_fixed_risk_eligibility`), `get_open_position_tickets`, `fetch_ohlc_data`, `get_live_spread` (technical R:R) and `get_symbol_execution_contract` (strategic and pre-route feasibility). The last two were ungated in the first build; a status that turned false after bars had been read could leave the current cycle with spread and contract data. Intentionally ungated:
| Reader | Why it stays available |
| --- | --- |
| `get_position_history_summary`, `get_trade_history_summary` (`_deals`) | They report deals the broker already booked. Reconciliation of closed positions must keep working while the EA cannot resolve symbols, and a failed symbol lookup says nothing about a past deal. They still need currency evidence (section 7). |
| `get_ea_profile` | `validation.file_bridge_check` and Policy-Live must be able to report `symbols_ok` false and the `symbol_errors` list. It never feeds a decision. |
| `_account_currency` / `_unit_scale` (private) | They only choose the money unit for the ungated history reads. |
| `get_preserve_10_approval_facts` | Returns the fixed unsupported failure; reads no file. |
**`symbol_errors` without `symbols_ok` (reviewer question).** `mt5_ea/FX_Execution.mq5` (read only) writes both in one statement block of the market status payload (`"symbols_ok"` then `"symbol_errors"`, lines 441-442). Both come from the same loop in `ResolveSymbols()` over `g_symbol_resolved[]`: `g_symbols_ok` is true exactly when no symbol failed and `BuildSymbolErrorsJson()` lists exactly the failed ones. Before the first `ResolveSymbols()` the global array is all false, so the EA reports `symbols_ok` false with all six names. The EA therefore always writes both and they agree. Python still gates on a non-empty `symbol_errors` when `symbols_ok` is absent, so a partial or hand-edited status cannot open the gate. A status with `symbols_ok` true and a non-empty list stays invalid (`FILE_BRIDGE_STATUS_FIELD_INVALID`), and an old EA that writes neither field behaves as before.
### 5. Margin-mode assumption
The connection does not gate on `account_margin_mode`. `validation.file_bridge_check` fails unless it normalises to hedging: ints 0, 1, 2 map to NETTING, EXCHANGE, HEDGING (the MT5 enum order), and strings containing HEDG, NETT or EXCHANGE (any case) map to the same names. Anything else fails as unrecognised. The EA track's representation is not fixed by the contract, so both forms are accepted; the EA track must confirm it (handoff item 6).
### 6. New execution statuses
`REJECTED_SYMBOL`, `REJECTED_ACCOUNT_MODE` and `REJECTED_LIVE_GUARD` are added to the `statuses` set in `core/schemas.validate_execution_feedback`, with the existing rule that a rejection carries ticket 0. No other file changes: `database/repositories/trades.py` (lines 210, 305) and `memory.py` (234, 313) treat every status starting with `REJECTED` as a definitive rejection (trade and reservation become REJECTED, reason code kept), and `application/feedback_service.consume_feedback` (line 394) releases the router lock for everything except `REJECTED_ORDER_SEND` and `EXECUTION_UNCERTAIN`. Tests cover the schema, the ledger, the reader and the engine lock release.
### 7. Unknown currency fails closed (review findings BR-001 to BR-006)
Scale 1.0 is a deliberate choice for a currency that is present and not a known cent denomination (USD, EUR, ...). It is never the answer for a currency that is missing, empty, unreadable or not yet read. Each path below returns no money value instead:
| Path | Currency evidence | While unknown |
| --- | --- | --- |
| `ExecutionFeedbackReader` (`collect_execution_feedback`, `collect_trade_exits`, `consume_*`, `read_execution_feedback`) | a readable `account_snapshot.json` with a non-empty `currency`, cached once seen | returns nothing, logs a warning, leaves every valid artifact in place for the next poll. Schema-invalid and malformed artifacts are still quarantined (moved, not deleted), as before. |
| `MT5Connection._unit_scale` (closed-position and trade summaries, `get_symbol_execution_contract`) | a successful `account_info()` with a non-empty `currency` | returns no summary or contract, sets `MT5_ACCOUNT_CURRENCY_UNAVAILABLE`; retried on the next poll |
| `FileBridgeConnection._account_currency` (same readers) | a fresh, valid `status.json` `account_currency` or a fresh account snapshot `currency`; the last validated value is the fallback; conflicting values count as unknown | returns no summary or contract, sets `FILE_BRIDGE_ACCOUNT_CURRENCY_UNKNOWN` |
| `research/demo_records.resolve_units` (R7, R8, R9) | `--account-currency`, else `status.json` `account_currency` beside the deals copy or in its parent | raises `UnitsUnknownError`; the three CLIs exit with code 2 and an explanation before writing any report |
| `check_bridge_health.py` | snapshot `currency` | prints no balance, `balance` is `None` |
Mock and USD runs. The mock feedback simulator writes dollars and no currency, and `main.py` builds the reader with `allow_mock_artifacts=self.use_mock`. The reader therefore treats a payload tagged `feedback_source: mock_feedback_simulator` as known USD when no snapshot currency exists, and only when `allow_mock_artifacts` is true; with it false such payloads are quarantined as before. Real EA artifacts always need a snapshot currency. A real USD account passes on the first poll because the EA writes `currency` "USD" into every snapshot (`FX_Execution.mq5` line 1255). When a snapshot currency exists it always wins, so the mock rule never rescales anything. No change to `main.py` or `application/**` is needed.
`read_execution_feedback()` (public, non-deleting) now returns `collect_execution_feedback()` payloads, so it converts exactly as the engine's path does. `check_bridge_health.py` converts the snapshot balance with `account_unit_scale`: 1000 USC prints `$10.00`.

## Rejected alternatives
- Convert `tick_value` only inside `evaluate_*`: leaves the engine snapshot in cents, so `account_status.balance` and every ratio against deal profit stay wrong.
- Keep Python in account currency and convert nothing: fixed-dollar policy, `FIXED_RISK_USD`, dashboards, `$` labels and research R would mix units.
- A new `core/money_units.py`: outside the owned-file list; the helpers sit next to the denomination map in `core/mt5_bridge.py`.
- Compute Preserve-10 margin in Python from an assumed leverage: an approximation in a fail-closed gate.
- Gate the connection on hedging: the representation is not agreed with the EA track, and a wrong guess would block a correct account. The checker reports it instead.
- Map symbols in Python: the EA places orders and exports data, so it owns the broker-name mapping; Python stays on canonical names.
## Consequences
- USD behaviour and output are unchanged (identity conversion, optional fields absent). 
- Cent accounts show dollars everywhere Python reads money, so `evaluate_trade_feasibility` approves the 25-pip 1.5% trade on $10 (lot 0.06).
- A suffixed account fails closed with `FILE_BRIDGE_SYMBOLS_NOT_OK` until the EA reports `symbols_ok` true; this depends on the EA track (C1 EA half).
- A present, unrecognised currency (for example EUR) is still scale 1.0 without an error. A missing or unreadable currency is unknown and blocks the money value (section 7); on a real bridge this defers feedback until the next snapshot, normally a few seconds.
- The cent `contract_size` (1,000) only matters to research, which expresses volume in standard-lot equivalents.
- Files outside this track's ownership that need follow-up are listed in `docs/handoff/2026-11-04-cent-bridge-handoff.md`.
