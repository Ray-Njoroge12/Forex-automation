# FX AI Engine (SRS v1)

Deterministic, local-hosted Forex automation engine aligned to `SRS_v1`.

## Scope
- Forex majors only
- Proprietary capital only
- Python intelligence + MT5 execution bridge
- No LLM/RL in v1

## Quickstart
1. Create a supported Python 3.11–3.13 environment and install the intended dependency set:
   - live engine: `pip install -r requirements/runtime.txt`
   - research/replay: `pip install -r requirements/research.txt`
   - optional sentiment research: `pip install -r requirements/optional-sentiment.txt`
2. Initialize project folders and DB:
   - `cd fx_ai_engine`
   - `python init_system.py`
3. Set MT5 credentials via environment variables:
   - `MT5_LOGIN`
   - `MT5_PASSWORD`
   - `MT5_SERVER`
4. Run bridge smoke test:
   - `python test_bridge.py`

## Tests
- `python -m pytest -q`

## Run Modes
- Smoke mode: `python main.py --mode smoke`
- Demo mode: `python main.py --mode demo`

Demo mode checks that the connected MT5 account is a demo account at startup
and on every processing loop. The EA also defaults to `RequireDemoAccount=true`.
Keep that setting enabled for demo testing and verify the account in MT5.

## Backtesting (Backtrader)
1. Use an OHLC CSV with columns: `time,open,high,low,close[,volume]`
2. Run:
   - `python -m backtesting.bt_runner --csv /path/to/EURUSD.csv --symbol EURUSD`

## Replay data preparation

Do not run optimisation against an unverified CSV. Validate each historical
input first and retain its manifest with the resulting replay report:

- `python -m validation.prepare_replay_dataset --csv /path/EURUSD.csv --symbol EURUSD --output artifacts/datasets/EURUSD.manifest.json`

The manifest records the source hash, coverage, rows, columns, and median bar
interval. A changed input produces a new manifest and must be treated as a new
experiment.

Candidate threshold changes are deliberately blocked unless the input manifest
matches the CSV, the data covers at least one year, and the realistic research
profile is used. This prevents smoke-test results from being treated as an
optimisation outcome:

- `python -m backtesting.threshold_audit --csv /path/EURUSD.csv --symbol EURUSD --policy-mode preserve_10 --dataset-manifest artifacts/datasets/EURUSD.manifest.json --tech-pullback-buffer-pips 8`

`backtesting.replay_execution` provides experimental bid/ask quote-bar fill
primitives with next-observation timing and conservative stop-gap handling.
These primitives are not yet connected to the strategy or portfolio replay;
the existing Backtrader runner and its results remain separate. Neither is a
verified production-equivalent replay or evidence for parameter promotion.

## Observability (Local Only)
Environment variables:
- `USE_MT5_MOCK=1` (use mock MT5 for tests/dev)
- `OTEL_ENABLED=1` (enable OpenTelemetry console tracing)
- `OTEL_SERVICE_NAME=fx_ai_engine` (optional override)
- `PROM_ENABLED=1` (enable Prometheus metrics)
- `PROM_PORT=8000` (metrics port)

Metrics endpoint:
- `curl http://localhost:8000/metrics`

## Market-context safety

New decisions fail closed when the bundled economic calendar does not cover at
least the next seven days. Stale rate differentials are disabled as a soft
modifier. Install sourced replacements only through the validation command:

- `python -m validation.update_market_context --calendar /path/to/calendar.json`
- `python -m validation.update_market_context --rates /path/to/rates.json`

Executable signals use schema version 2, an expiring policy envelope, and an
atomic portfolio reservation. The MT5 EA must be rebuilt after envelope changes.
Signal expiry alone does not release an unresolved reservation: the order may
already have reached the broker. Reserved, routed, and execution-uncertain
entries continue to consume capacity until a terminal outcome or verified
reconciliation resolves them. Investigate stalled reservations against the
MT5 order and position history before any manual state change.

Execution feedback is applied with its reservation, lifecycle event, and
deduplication receipt in one database transaction. Invalid feedback is
quarantined; unresolved conflicts and uncertain order sends halt new entries.
Inspect `execution_feedback_conflicts`, quarantined `execution_*.json` files,
and the broker's orders, deals, and positions before resolving a halt. Do not
clear the records merely to resume trading. Records produced by older versions,
including prior `REJECTED_ORDER_SEND` outcomes, require separate review.

Build 2 still needs terminal evidence for MT5 order-send return codes, partial
fills, delayed feedback, broker history and restarts, plus a documented,
evidence-backed resolution workflow. The Python tests do not validate those
broker behaviors or establish demo readiness.

The EA now reports an uncertain outcome unless a market request reports a
completed server result, deal ID, positive fill price and matching fill volume.
Pending limit orders and partial fills require reconciliation. The EA writes a
submission marker before sending and holds repeated signals for reconciliation
across restarts. This assumes only one EA instance uses the bridge directory;
the marker is not a cross-process lock. Compile and test restart behavior on the
intended demo terminal before relying on this control. Do not remove markers
without reviewing broker orders, deals and positions.

For a read-only local inventory of active reservations and unresolved feedback
conflicts, run `python -m validation.reconciliation_inventory`. The output
includes unscoped orphan reservations and explicitly marks broker evidence as
unverified. It does not query MT5 or resolve anything. Compare its identifiers
with the correct account's current orders, deals, positions and terminal logs;
retain that evidence and keep trading halted when it is incomplete.
Pass `--bridge-dir /path/to/bridge` to include pending signals, submission
markers, and quarantined execution feedback in the inventory.

## Immediate readiness gates

On macOS, run `python -m validation.mt5_background_preflight` for a read-only
inventory of the installed terminal, candidate MQL5 locations and Python
binding availability. It does not launch MT5, identify the active account or
prove that a candidate directory is the terminal's current data folder.

Before tuning any strategy parameter, install current sourced market context,
compile and demo-test the EA, and produce a hash-manifested replay baseline.
The detailed acceptance checklist is in
`docs/plans/2026-08-31-immediate-priorities-delivery-plan.md`.

Check local prerequisites at any time with:

- `python -m validation.readiness_preflight --strict`

Use `--json` when storing the result with deployment or demo-validation evidence.

## EA deployment pin

The EA now fails closed unless `ExpectedPolicyHash` is configured (the only
exception is an explicit development override of `RequirePolicyHashPin`). Before
attaching the EA to a demo terminal, print the hash for the selected policy:

- `python -m validation.print_policy_hash --mode core_srs`

Set the returned value in the EA's `ExpectedPolicyHash` input, rebuild the EA,
and retain the command output with the demo deployment evidence.

Generate the manual EA acceptance-test payloads only for a disposable demo
terminal. Do not copy the `valid` case to a funded account:

- `python -m validation.ea_demo_matrix --output-dir artifacts/ea-demo-matrix --mode core_srs`

The resulting `manifest.json` defines the expected result and any required
terminal setup for each case. Run one case at a time and retain MT5 logs,
feedback files, and the matching Python lifecycle records as evidence.
