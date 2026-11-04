# ADR 0012: Cent-account money units and broker symbol suffixes on the file bridge
Date: 2026-11-04. Status: accepted. Branch `cent/bridge-track` (base `71bb25f`). `bridge/**` is a protected path: second-model review and owner approval are required before merge (the owner approved building on 2026-11-04).
## Context
`docs/STATUS.md` issue 27 and `fx_ai_engine/docs/runbooks/cent-account-setup.md` section 0 record what failed on a temporary USC copy of the bridge:
- A cent account reports balance, equity, margin, profit and tick value in cents (currency `USC`, 1,000 USC = $10). The file bridge passed the snapshot through in cents, while `evaluate_trade_feasibility` and `evaluate_fixed_risk_eligibility` converted the balance to dollars and read `tick_value` in cents. A 25-pip, 1.5% trade on $10 got `REJECTED_LOT_PREROUTE` with estimated lot 0.0006 instead of 0.06.
- Brokers such as Exness Standard Cent suffix symbols (`EURUSDc`). The EA and the engine hard-code the six plain names.
- `preserve_10` cannot start on the file bridge (`FILE_BRIDGE_PRESERVE_10_UNSUPPORTED`).
## Decision
### 1. One money-unit boundary: ingest
Every money value that leaves the bridge layer is US dollars. Lots stay broker lots. Prices, pips and ratios are never scaled. The scale is `account_unit_scale(currency)` from `_ACCOUNT_DENOMINATION_MAP` in `core/mt5_bridge.py` (USD 1.0; USC, USCENT, USDCENT, CENTUSD 0.01; any other currency 1.0, as before, with no new error). Conversion is `usd_from_account_units(value, scale)`: `round(value * scale, 8)` when the scale is not 1.0, the identity otherwise, so USD output is byte for byte unchanged. When the scale is not 1.0 a snapshot dict gains `money_unit: "USD"` and `unit_scale`; for USD these keys are absent. `currency` stays as the broker reported it.
| Raw source | Converted in | Fields |
| --- | --- | --- |
| `MT5Connection.get_account_snapshot` | `core/mt5_bridge.py` | balance, equity, margin_free, floating_pnl |
| `FileBridgeConnection.get_account_snapshot` | `core/file_bridge_connection.py` | same four |
| `ExecutionFeedbackReader.read_account_snapshot` (feeds `update_account_state`) | `bridge/execution_feedback.py` | same four |
| exit_*.json and execution_*.json | `bridge/execution_feedback.py` | `profit_loss`, scale from the sibling `account_snapshot.json` currency (last good value cached, 1.0 if never readable) |
| `get_symbol_execution_contract` (both connections) | `core/mt5_bridge.py`, `core/file_bridge_connection.py` | `tick_value`. This fixes both `evaluate_*` checks without changing their math |
| Deal summaries (both connections) | `core/mt5_bridge.py` `_closed_history_summary_from_deals(unit_scale=)` | per-leg and total `profit_loss` |
| Offline copies of bridge files | `research/demo_records.py` loaders | deal money fields, exit and execution `profit_loss`, deal volume in standard-lot equivalents (cent contract size 1,000 vs 100,000) |
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
| `symbols_ok` is false | every read fails closed; message lists `symbol_errors` (or `unspecified`) | `FILE_BRIDGE_SYMBOLS_NOT_OK` |
| `account_currency` | non-empty string | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| `max_spread_pips_input` | finite number >= 0, not a bool | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| `live_guard` | object | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| `account_margin_mode` | string or int (shape only) | `FILE_BRIDGE_STATUS_FIELD_INVALID` |
| status vs snapshot currency | both present and different (case-insensitive) | `FILE_BRIDGE_ACCOUNT_CURRENCY_MISMATCH` |
| `symbols.json` `broker_symbol` | non-empty string when present | `FILE_BRIDGE_STATUS_FIELD_INVALID` (wrapped by `FILE_BRIDGE_SYMBOL_SPEC_UNAVAILABLE`, precise code kept) |
`FileBridgeDataError` now carries an optional `reason_code`; `_code_for(exc, default)` keeps the precise code when the error is turned into a `BridgeError`. The Python side keeps canonical names (`EURUSD`) everywhere; file names and bar payload symbols stay canonical, and the EA owns the mapping to the broker symbol. `FileBridgeConnection.get_ea_profile()` returns the validated fields (None when absent) without the `symbols_ok` gate, for `validation.file_bridge_check` and for Policy-Live.
### 5. Margin-mode assumption
The connection does not gate on `account_margin_mode`. `validation.file_bridge_check` fails unless it normalises to hedging: ints 0, 1, 2 map to NETTING, EXCHANGE, HEDGING (the MT5 enum order), and strings containing HEDG, NETT or EXCHANGE (any case) map to the same names. Anything else fails as unrecognised. The EA track's representation is not fixed by the contract, so both forms are accepted; the EA track must confirm it (handoff item 6).
### 6. New execution statuses
`REJECTED_SYMBOL`, `REJECTED_ACCOUNT_MODE` and `REJECTED_LIVE_GUARD` are added to the `statuses` set in `core/schemas.validate_execution_feedback`, with the existing rule that a rejection carries ticket 0. No other file changes: `database/repositories/trades.py` (lines 210, 305) and `memory.py` (234, 313) treat every status starting with `REJECTED` as a definitive rejection (trade and reservation become REJECTED, reason code kept), and `application/feedback_service.consume_feedback` (line 394) releases the router lock for everything except `REJECTED_ORDER_SEND` and `EXECUTION_UNCERTAIN`. Tests cover the schema, the ledger, the reader and the engine lock release.
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
- Unknown currencies are still treated as scale 1.0 without an error.
- The cent `contract_size` (1,000) only matters to research, which expresses volume in standard-lot equivalents.
- Files outside this track's ownership that need follow-up are listed in `docs/handoffs/2026-11-04-cent-bridge-handoff.md`.
