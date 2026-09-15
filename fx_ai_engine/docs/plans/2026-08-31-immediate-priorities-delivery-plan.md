# Immediate Priorities Delivery Plan

**Status:** In progress — repository-owned controls are being implemented; two
operator-owned gates remain.

## Objective

Make optimisation evidence reproducible before altering strategy thresholds,
and ensure the execution path is demonstrably ready for demo validation.

## Workstream A — completed in this delivery

1. Split production dependencies from research and optional sentiment packages.
2. Validate OHLC data and produce a hash-based replay manifest before each run.
3. Make the standard operating steps explicit in the README and acceptance
   checklist below.

## Workstream B — operator gate: market context

1. Obtain licensed, reviewed calendar and policy-rate inputs.
2. Confirm calendar coverage includes at least the next seven days.
3. Install only through `validation.update_market_context`.
4. Archive source, provenance, retrieval time, and the generated manifest with
   the future replay report.

**Exit criterion:** the engine starts without a calendar freshness rejection.

## Workstream C — operator gate: EA validation

1. Compile `mt5_ea/FX_Execution.mq5` in the intended Windows MT5 terminal.
2. Record MetaEditor build output and EA source revision.
3. Use a demo account to test valid, malformed, expired, duplicate,
   over-spread, and over-capacity signal payloads.
4. Reconcile every EA feedback file with the Python lifecycle event table.

**Exit criterion:** every rejection has the expected reason code and valid
signals respect the same policy hash, expiry, spread, and capacity limits as
the Python engine.

## Workstream D — first replay baseline

1. Run `prepare_replay_dataset` for every input pair/timeframe.
2. Store manifests, frozen policy, explicit cost assumptions, decisions,
   fills, equity curve, and risk events in one replay-run directory.
3. Analyse performance per symbol, market regime, session, and cost scenario.
4. Require out-of-sample, walk-forward, and adverse-cost checks before any
   parameter is promoted.

**Exit criterion:** an independently rerunnable baseline report exists. It is
not an approval for live capital.
