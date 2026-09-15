# Maintainability, Organisation, and Cleanup Audit

**Date:** 2026-08-31  
**Scope:** Post-hardening review of the Forex automation project, focused on
operability, project organisation, error containment, and the next optimisation
cycle.

## Current assessment

The project is now materially safer to operate than the original baseline. The
first hardening cycle closes the most important execution-boundary gaps:

- every routed signal now carries a versioned execution envelope;
- trade reservations protect account-level limits across concurrent same-cycle
  decisions;
- the Python router and EA both validate expiry, spread, and capacity limits;
- only completed MT5 bars are used for signal inputs;
- stale calendar data stops new decisions, while stale rate data disables the
  non-essential rate-differential modifier;
- signal and lifecycle state are stored in migration-managed tables.

The strategy has **not** yet been demonstrated profitable or robust. The
repository does not contain sufficient current market data, execution history,
or a production-faithful replay result to make that claim. Treat it as a
development system until the validation work below is complete.

## Cleanup completed in this cycle

| Area | Change | Effect |
| --- | --- | --- |
| Database setup | `init_system.py` now uses the current migration runner | Fresh installations no longer receive a stale, partial schema. |
| SQLite behaviour | Connections enable foreign keys and a busy timeout; schema initialisation enables WAL | Better integrity and resilience when the engine, router, and monitoring processes overlap. |
| Signal routing | Database duplicate-check failures are fail-closed in the live engine | A database outage cannot silently disable deduplication. |
| Runtime watchdog | UTC-safe health timestamps, targeted process errors, and local MT5 path resolution | Fewer false restarts and less hidden operational failure. |
| Documentation | Added project-structure guidance and this cleanup audit | The intended module boundaries and safe cleanup order are explicit. |
| Tests | Added initialisation/migration coverage alongside the earlier optimisation tests | Current schema bootstrapping is verified. |

## Remaining structural weaknesses

### 1. Overloaded composition root — priority P1

`main.py` is approximately 2,277 lines and mixes startup, data collection,
policy evaluation, routing, feedback handling, and monitoring. This makes
isolated tests and safe changes expensive.

**Required change:** retain `main.py` as a thin composition root and extract:

- `application/decision_cycle.py` — one-symbol evaluation;
- `application/market_data_service.py` — MT5 retrieval and freshness checks;
- `application/feedback_service.py` — EA acknowledgements, fills, and exits;
- `application/engine_runtime.py` — scheduling and graceful shutdown.

Keep behaviour unchanged during extraction. Add contract tests before moving
each responsibility.

### 2. Overloaded persistence module — priority P1

`database/db.py` is approximately 1,389 lines. It currently contains schema
initialisation, migrations, connection logic, trade queries, reservation
operations, analytics, and configuration persistence.

**Required change:** split it without changing the public calls in one step:

- `database/connection.py` — connection and transaction helpers;
- `database/migrations.py` — current migration runner;
- `database/repositories/trades.py` — proposals, fills, and lifecycle events;
- `database/repositories/risk.py` — reservations and account exposure;
- `database/repositories/config.py` — settings and strategy configuration.

Use explicit repository interfaces for the application layer so replay tests
can use an in-memory implementation.

### 3. Dependency and runtime reproducibility — priority P1

`requirements.txt` combines the live runtime with notebooks, dashboards,
telemetry, transformers, and sentiment tooling. The checked virtual environment
is Python 3.14 while project guidance references Python 3.11, and its `pip`
module is unavailable. This is a reproducibility risk, not merely cleanup.

**Required change:** publish pinned, separately installable requirement sets:

- `requirements/runtime.txt` — required live-engine packages only;
- `requirements/research.txt` — replay, notebook, and analysis packages;
- `requirements/optional-sentiment.txt` — transformers and feed ingestion;
- a tested Python version range and bootstrap command.

Add a CI job that creates a clean environment from the runtime set, imports the
engine, and runs the non-MT5 test suite.

### 4. Market-data operations are currently blocked — priority P0

The checked calendar data ends in June 2026 and the rate data is from February
2026. The new freshness gate correctly prevents the system from opening new
trades under that condition.

**Required operator action:** obtain reviewed, licensed calendar and rate
sources, then install them through:

```text
python -m validation.update_market_context --help
```

Do not edit the packaged datasets manually. Record source, retrieval time,
coverage, and review outcome in the generated metadata.

### 5. EA build and parsing assurance — priority P1

The MQL5 EA now performs stronger envelope checks, but MetaEditor is not
available in this development environment. Its hand-built JSON parsing remains
an avoidable source of malformed-message risk.

**Required change:** compile `mt5_ea/FX_Execution.mq5` in a Windows MT5
environment, execute a sandbox/demo test matrix, and add a CI-compatible
compile check where feasible. Test malformed, expired, duplicate, over-spread,
and over-capacity payloads explicitly.

### 6. Strategy-validation fidelity — priority P0/P1

The new chronological replay utility is a foundation, not proof. The existing
backtest paths still need a shared execution-cost model, historical spread and
slippage assumptions, event-time economic-calendar data, and output that can
be reconciled with EA fills.

Before changing model thresholds, validate every candidate across:

1. in-sample development;
2. untouched out-of-sample periods;
3. walk-forward windows;
4. Monte Carlo trade-order and cost perturbations;
5. demo forward trading with the same policy version and risk caps.

Promote a configuration only if it meets predefined return, drawdown,
turnover, exposure, and stability guardrails. Do not optimise a single overall
profit number.

### 7. Operational cleanup to defer until references are mapped — priority P2

The repository root contains diagnostics, reports, and one-off operator scripts.
Moving them now could break scheduled tasks or runbooks. First search scheduler
configuration and documentation references, then move only confirmed standalone
tools into `tools/diagnostics/`, `tools/reports/`, and `scripts/operations/`.
`docs/PROJECT_STRUCTURE.md` provides the target arrangement.

## Optimisation sequence

| Order | Deliverable | Exit criterion |
| --- | --- | --- |
| 0 | Install current reviewed market context; compile and demo-test EA | Engine passes freshness gates; EA build/test evidence recorded. |
| 1 | Produce a baseline replay report from historical data | Reproducible dataset manifest, assumptions, and per-symbol performance table. |
| 2 | Extract application services and repositories | No behavioural change; existing and new contract tests pass. |
| 3 | Split/pin runtime dependencies and add clean-environment CI | Fresh supported interpreter runs tests and imports the engine. |
| 4 | Calibrate only evidence-backed parameters | Walk-forward and stress results meet agreed guardrails. |
| 5 | Demo canary and live-readiness review | Reconciled EA/Python audit trail and sustained risk-compliant demo results. |

## Verification performed

- `python -m pytest -q`: **236 passed**.
- Python bytecode compilation completed for the updated packages.
- `git diff --check` reported no whitespace errors.
- The market-context installer help command ran successfully.
- `init_system.py` was run against the project database and created the current
  migration-managed schema. The resulting runtime database is ignored by Git.

## Decision record

The recommended next development cycle is **validation infrastructure and
structural extraction**, not new entry logic or machine-learning complexity.
The current evidence can support hardening and reproducibility work; it cannot
yet support a claim that any proposed strategy optimisation improves real-world
risk-adjusted performance.
