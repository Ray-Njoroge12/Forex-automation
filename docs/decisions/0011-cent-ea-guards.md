# ADR 0011: EA guards for cent accounts and a guarded real account (C1, C4, C5 EA part)

Date: 2026-11-04. Status: built on branch `cent/ea-track`, not merged, not deployed. Needs a review by a different model than the coder and the owner's approval before merge (protected path `mt5_ea/**`, AGENTS.md).

## Context

`fx_ai_engine/docs/runbooks/cent-account-setup.md` found that the system cannot trade a cent account: the EA hard-codes the six canonical symbol names (finding 1), refuses every real account with no way to test its own guards (finding 4), has a spread cap that cent spreads may trip (finding 5), and never reads the margin mode (finding 6). The owner approved the fixes C1 to C5 on 2026-11-04. This ADR covers the EA part of C1, C4 and C5 (EA build `2026.11.04-2`). The Bridge track (Python file bridge, cent units) and the Policy-Live track (micro policy, live mode) are separate.

## Decision

### C1 Symbol suffix

- New input `SymbolSuffix` (string, default `""`). The broker name is `canonical + SymbolSuffix`.
- Canonical names (`EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`, `USDCHF`) stay everywhere the EA writes: bar file names `bars_<CANONICAL>_<TF>.json` and their `symbol` field, the keys of `symbols.json` and `latest_closed_bars`, `symbol` in `deals.json` and `market_quotes.json`, `open_symbols` in the account snapshot. Execution and exit feedback carry no symbol field.
- The broker name is used for every MT5 call: `SymbolSelect`, `iTime`, `CopyRates`, `SymbolInfo*`, `SymbolInfoTick`, order check and send, position reads, ATR. Deal symbols read from history are mapped back to canonical names by exact match against `canonical + suffix`; positions on other symbols are passed through unchanged, not suffix-stripped.
- `symbols.json` entries gain a last field `broker_symbol`.
- `status.json` gains `symbol_suffix`, `symbols_ok` (true only when all six select) and `symbol_errors` (canonical names that did not select). The set is refreshed at start and every 60 seconds with the spec export, and again for each signal.
- If any of the six does not resolve, signal processing writes feedback `REJECTED_SYMBOL` and deletes the signal.
- With an empty suffix `BrokerSymbol(x) == x` and `CanonicalSymbol(x) == x`, so every exported name equals what the previous build wrote. Only the new fields are additions.

### C4 Account guards

- `status.json` gains `account_margin_mode` (`hedging`, `netting`, `exchange`, or `unknown`), `account_currency` and `max_spread_pips_input`.
- A signal is processed only on a hedging account. Otherwise: feedback `REJECTED_ACCOUNT_MODE`, signal deleted. Python releases the reservation (Bridge track).
- Spread policy is unchanged. The EA still rejects above `min(MaxSpreadPips, signal max_spread_pips)`, and the input default stays `2.0`. A cent account gets its ceiling by setting `MaxSpreadPips` when the EA is attached. The policy's own cap rides in the signal. `max_spread_pips_input` lets the operator and Python see the ceiling in force.

### C5 Live-account guard (EA part)

- New inputs: `AllowLiveAccount` (bool, default false), `LiveMaxLotPerOrder` (double, default 0.0), `LiveMaxOrdersPerDay` (int, default 0). `RequireDemoAccount` stays true by default.
- On a real account (`ACCOUNT_TRADE_MODE != DEMO`) a signal is processed only if `RequireDemoAccount` is false, `AllowLiveAccount` is true, `LiveMaxLotPerOrder > 0`, `LiveMaxOrdersPerDay > 0`, and `<RootFolder>\KILL_SWITCH` does not exist. Otherwise: feedback `REJECTED_LIVE_GUARD`, signal deleted, and the exact reason in `ea_debug.log` (`Live guard reject: ...`).
- The kill switch also blocks new orders on a demo account, so it can be tested there. It is read only in the signal path. It never closes a position and does not touch `ManageOpenPositions`, the closed-position sweep or exit finalisation.
- Lot ceiling: when `LiveMaxLotPerOrder > 0` and the final lot is above it, the lot becomes the ceiling stepped down to the broker lot step (`floor(cap / step + 1e-9) * step`; the epsilon avoids `0.03 / 0.01` evaluating to 2.999...). It never raises a lot. If the stepped-down lot is below the broker minimum the signal is rejected `REJECTED_LIVE_GUARD`.
- Daily order cap: `feedback/live_orders_day.json` holds `{"utc_day":"YYYY-MM-DD","orders":N,"last_trade_id":"..."}`. The count is read at admission and incremented (atomic publish, then read back and compared) after the preflight and before the submission marker is written. Any read, write or verification failure, or an unavailable clock, rejects with `REJECTED_LIVE_GUARD` (fail closed). The file is validated strictly on every read: `utc_day` must be a real `YYYY-MM-DD` date, `orders` must be digits only (no sign, point or space, at most 9 digits), and `last_trade_id` must be non-empty. Anything else is a read failure, not a rollover. Only a valid file from an earlier day resets the count to 0. A valid file dated after today (clock rolled back, damaged file) is a read failure too, so it cannot reopen the cap. The count is per trade id: the admission gate lets a signal whose trade id is the one already counted today pass the cap check, and `RecordLiveOrderIntent` does not count it again. So if the counter reaches the cap and the submission-marker write then fails, the retry of that same signal is still processed, and a different signal is rejected. It counts order intents, not fills: an order that ends `EXECUTION_UNCERTAIN` or is rejected by the broker still uses one slot. The file name does not start with `execution_`, which the Python bridge globs.
- The kill switch is checked three times in the signal path: in the admission gate, again after the preflight and before the counter is touched (a blocked order then uses no cap slot), and a last time after the submission marker is verified, immediately before the `CTrade` calls. If the last check trips, the EA deletes the marker, logs `kill switch file present at send boundary`, writes `REJECTED_LIVE_GUARD` and consumes the signal; no request was sent. The counter already holds that trade id and is not rolled back, so that one slot stays used (it counts intents, and an over-count only reduces risk). If the marker cannot be deleted the EA logs a warning and still rejects, since the signal file is gone and nothing reads an orphan marker. A switch created after the last check cannot be stopped; that window is the few lines between the check and `CTrade`.
- `status.json` gains `live_guard`: `account_real`, `require_demo`, `allow_live`, `max_lot`, `max_orders_per_day`, `orders_today` (`-1` when the counter file exists but cannot be read), `kill_switch`.

### Readings chosen where the task text was silent

1. A real account with the defaults used to return at the top of `ProcessPendingSignal` and leave the signal file in place. It now reaches the live guard and gets `REJECTED_LIVE_GUARD` with the file consumed, as the task requires. Demo accounts with the defaults are unaffected.
2. The lot ceiling and the daily cap apply on any account when their input is greater than 0, so the matrix can run them on the demo. With the defaults (0) they do nothing on demo. On a real account both must be greater than 0 anyway.
3. A signal for a symbol outside the six keeps today's path: `SymbolSelect` fails on the translated name, the EA logs it and deletes the file with no feedback. Only "any of the six unresolved" gets `REJECTED_SYMBOL`. There is no canonical allow-list.
4. A margin mode outside the three known values is reported as `unknown` and treated as not hedging (`REJECTED_ACCOUNT_MODE`).
5. Gate order in `ProcessPendingSignal`, after the unchanged envelope checks and expiry: `REJECTED_ACCOUNT_MODE`, `REJECTED_LIVE_GUARD`, `REJECTED_SYMBOL`, then the existing spread, position-limit and lot checks. The lot ceiling follows the existing minimum-lot check. The counter is recorded after the preflight and before the submission marker, so a counter failure cannot leave a marker that later reads as `EXECUTION_UNCERTAIN`.
6. Rejections follow `REJECTED_POLICY_HASH`: `WriteExecutionFeedback(...)`, `FileDelete`, `return`, no `MarkProcessed`.
7. The kill switch is tested with `FileIsExist`, which cannot tell "absent" from "unreadable". That is the only fail-open edge in the guard.

## What does not change

- Every `Envelope fail` check, the policy-hash logic, `ExpectedPolicyHash` (still the core_srs hash `f00f1f9c3e9120971378f3c1eb95d28f09d1d5e2ee97035a7f4c00a998535dc8`) and `RequirePolicyHashPin`.
- `MaxSpreadPips = 2.0` and `MathMin(MaxSpreadPips, signalMaxSpreadPips)`.
- Position management (break-even, partial close, ATR trail), exit finalisation, the calendar export (keyed on currencies, not symbols).
- AGENTS.md invariants: only the EA places orders, and these guards only reject or reduce risk.

## Verification

Build `2026.11.04-2`, staging compile with `scripts/mt5/compile_ea.sh` (no `--deploy`), after the second review-fix pass (future-dated counter fails closed, kill switch re-checked before the counter and at the send boundary):

```
Result: 0 errors, 0 warnings, 8594 ms elapsed, cpu='X64 Regular'
27a410354da6f9318c87faa6bcf0523eac5a8901ab1539f8f60c2bd4fd49cd4c  fx_ai_engine/mt5_ea/FX_Execution.mq5
b67640e1a4777466399e579a73c752bd4254367a5a0905a7a0c185039b389f9c  fx_ai_engine/artifacts/mt5-build/FX_Execution.ex5
```

The first review round covered source `249d194a...` and `.ex5` `027548da...`; the second covered source `30a4f299...` and `.ex5` `2921ebf6...`. Both are superseded by the hashes above.

The unmodified source also compiled with 0 errors and 0 warnings before the changes (source `04f5c72a...`). The compile log is `fx_ai_engine/artifacts/mt5-build/compile.txt` (not committed). Nothing was deployed, attached or restarted, and no matrix case was run.

Tests, from `fx_ai_engine/`: `USE_MT5_MOCK=1 ../.venv/bin/python -m pytest -q` gives 634 passed (581 before). New: `tests/test_ea_cent_guards.py` (47 source-guard tests, including `test_empty_suffix_leaves_exported_names_unchanged` and `test_envelope_checks_unchanged`) and `tests/test_ea_demo_matrix_cases.py` (6 tests). `validation/ea_demo_matrix.py` gains `build_cent_guard_cases` (7 cases: `suffix_resolved`, `symbol_missing`, `account_mode_netting`, `kill_switch`, `live_lot_ceiling`, `live_lot_below_minimum`, `live_daily_cap`) and writes `required_terminal_setup` for all 14 cases in `manifest.json`. `build_demo_matrix` still returns the original seven.

### Verified only by compile plus source guards (owner must verify on the terminal after deploying)

- Suffixed symbol selection, specs, bars, ticks and order send on a broker with suffixed symbols, and the canonical-name outputs with a real suffix.
- `REJECTED_SYMBOL`, `REJECTED_ACCOUNT_MODE` and `REJECTED_LIVE_GUARD` feedback reaching Python.
- That MetaQuotes-Demo reports `hedging` (the new guard rejects every signal otherwise).
- The real-account path with all live conditions met.
- The counter file across an EA restart and a UTC day change, and that a future-dated counter file rejects.
- That creating `KILL_SWITCH` between admission and send rejects with `REJECTED_LIVE_GUARD` (a race; the source guards assert the checks and their order only).
- The kill switch leaving open positions managed. The `kill_switch` matrix case now requires the `valid` case's position to be open and managed, and passes that part only if `ea_debug.log` shows a management line for the ticket after the file was created; without a price trigger it is inconclusive.
- The strict counter parse (`IsValidUtcDay`, `ParseStrictCount`) on real MQL5 string and date behaviour, and the same-trade retry through the cap gate. The source guards assert structure and order only.
- The lot ceiling step-down against a real lot step.
- That Python accepts the `unknown` margin-mode value.

## Runbook note (handoff)

This track does not edit `fx_ai_engine/docs/runbooks/cent-account-setup.md` (outside its file ownership). The operator text for build `2026.11.04-2` is here instead. Inputs: `SymbolSuffix`; `MaxSpreadPips` (default 2.0 unchanged; a cent account gets its ceiling by setting it when the EA is attached; the lower of the input and the signal cap wins; shown as `max_spread_pips_input`); `AllowLiveAccount`; `LiveMaxLotPerOrder`; `LiveMaxOrdersPerDay`. The `KILL_SWITCH` file in the bridge root next to `ea_debug.log` blocks new orders on demo and real accounts, positions stay managed, and deleting it resumes. After attaching, check in `market/status.json`: `symbol_suffix`, `symbols_ok` (true), `symbol_errors` (empty), `account_margin_mode` (must be `hedging`), `account_currency`, `max_spread_pips_input`, `live_guard`. `symbols.json` entries show `broker_symbol`. The daily count is in `feedback/live_orders_day.json`. Matrix cases list their setup in `manifest.json` under `required_terminal_setup`. Sections 2 and 3 of the runbook still describe build `2026.11.04-1` until the owner deploys this build. The orchestrator or the owning track should add this to the runbook.

## Open

- Review by a different model than the coder: pending. The reviewer must state which model family reviewed and whether it matched the coder. This track does not merge.
- Owner approval before merge and before any deploy.
- Bridge track: handling of the three new feedback statuses and the new `status.json` fields.
- Policy-Live track: live mode and any micro policy. A new policy hash needs a new `ExpectedPolicyHash` pin and a recompile.
