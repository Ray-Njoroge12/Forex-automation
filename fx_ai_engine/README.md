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

## Immediate readiness gates

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
