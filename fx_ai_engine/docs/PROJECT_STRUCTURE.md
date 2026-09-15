# Project Structure and Cleanup Guide

## Runtime path

`main.py` is the composition root. It loads policy and environment values, runs
all migrations, creates the engine, and owns the long-running process. New
business logic should not be added directly to it.

```
application/     orchestration services (reserve, route, lifecycle coordination)
core/            deterministic domain logic and typed contracts
  agents/        regime, technical, adversarial, portfolio decisions
  risk/          hard limits, exposure, reset rules
  policy/        versioned signal envelope and policy identity
  data_quality/  freshness and completeness gates
bridge/          file outbox/inbox, MT5 feedback and mock adapter
database/        schema, migrations, lifecycle evidence
mt5_ea/          the broker-side execution and trade-management guard
backtesting/     historical/replay adapters; never a second live strategy
validation/      operational checks and controlled market-context import
ml/              offline/shadow research only; not a default live dependency
```

## Root utility scripts

The root-level scripts remain executable entry points for operator convenience:

- `check_bridge_health.py`, `check_paths.py`, `check_limits.py`, and
  `check_spreads.py`: connection/deployment diagnostics.
- `review_trades.py`, `spread_audit.py`, `analyze_tendencies.py`, and
  `audit_rejected_setups.py`: evidence review.
- `watchdog.py`: Windows-only process supervision.
- `init_system.py`: directory and fully migrated database bootstrap.

Do not move these scripts until their scheduled-task/XML references and
operator documentation have been updated together.

## Cleanup rules

1. New database changes require schema, idempotent migration, and upgrade test.
2. New executable signals must use the policy envelope and a reservation; no
   bridge bypasses are permitted.
3. Domain code returns typed decisions/reason codes; only application/bridge
   code reads files, writes SQL, or talks to MT5.
4. Keep production control dependencies minimal. ML and sentiment are optional
   research paths and must fail safely without affecting deterministic routing.
5. Put new operator reports in `validation/` or an explicit report package,
   not in `main.py`.
6. Archive superseded plans rather than deleting them; mark their status and
   link the replacement plan.

## Known next cleanup candidates

- Extract remaining decision-cycle logic from `main.py` into application
  services once its existing behavior is covered by integration tests.
- Replace hand-parsed EA JSON with a constrained robust parser or a generated
  fixed schema; this needs MT5 compilation/demo verification.
- Split optional ML/sentiment dependencies from the minimal engine install.
- Replace static UTC session windows with named-zone/DST-aware market sessions.
