# System Optimisation and Architecture Plan

**Date:** 2026-08-27  
**Status:** Immediate development cycle implemented; historical replay and MT5 demo acceptance remain evidence gates  
**Basis:** [System audit](../2026-08-27-system-audit.md), SRS v1, and the existing pre-live/trade-management plans.

## 1. Objective and operating principle

Bring the engine to a safe, measurable, and maintainable state before attempting return optimization. The sequence is:

```
execution safety -> trustworthy state/data -> production-faithful replay
  -> frozen strategy experiments -> forward demo -> controlled promotion
```

The project must not optimize thresholds to improve a simplified backtest before its order, cost, portfolio, and data controls match production. A stronger entry rule cannot compensate for a cap that the execution path can bypass.

## 2. Target structural arrangement

The current layered design is sound, but responsibilities need clearer ownership and a single source of truth for policy, risk, and trade lifecycle.

```
                         +------------------------+
                         | Versioned policy bundle |
                         | limits + TTL + schema   |
                         +-----------+------------+
                                     |
Market/broker data --> Data-quality gate --> Decision pipeline
                     (freshness/closed bars)   regime -> technical -> challenge
                                                        |
                                     +------------------v-----------------+
                                     | Portfolio reservation service       |
                                     | broker OPEN + DB PENDING/UNCERTAIN  |
                                     +------------------+-----------------+
                                                        |
                                     +------------------v-----------------+
                                     | Canonical risk and sizing service   |
                                     | broker contract + cost envelope     |
                                     +------------------+-----------------+
                                                        |
                                      durable trade state + signed envelope
                                                        |
                   +-------------------- Bridge outbox/inbox --------------------+
                   | idempotency, TTL, acknowledgements, reconciliation, retry    |
                   +------------------------------+--------------------------------+
                                                  |
                                          MT5 EA execution guard
                      policy/version/TTL/spread/position-count/broker-rule checks
                                                  |
                                 fills, exits, account snapshots, retcodes
                                                  |
                                      immutable event/audit store
                                                  |
                                dashboard + validation + replay research
```

### Structural boundaries

| Module area | Owns | Must not own |
|---|---|---|
| `core/data_quality/` (new) | Closed-bar checks, quote/snapshot/calendar freshness, broker symbol facts. | Signal logic or sizing. |
| `core/policy/` (new; extracted from config) | Typed versioned policy, validation, canonical hash. | Runtime mutable overrides in core SRS. |
| `core/strategy/` (reorganize agents) | Pure feature/strategy functions returning typed proposals and reasons. | Files, DB writes, MT5 calls. |
| `core/risk/` | Portfolio reservations, hard limits, broker-aware sizing, exposure. | Technical entry criteria. |
| `application/engine.py` (new; extract from `main.py`) | Cycle orchestration and explicit state transitions. | Indicator/SQL/JSON implementation details. |
| `bridge/` | Durable outbox/inbox transport and idempotency. | Re-deciding risk after reservation. |
| `database/` | Append-only decision/execution events and materialized lifecycle state. | Trading rules. |
| `backtesting/` | Same strategy/risk/sizing interfaces under an event replay adapter. | Its own duplicate strategy behavior. |
| `mt5_ea/` | Broker execution and independent last-line safety validation. | Strategy generation. |

The existing code can move toward this incrementally; no large rewrite is required. Preserve public contracts around `TechnicalSignal`, feedback payloads, and the current SQLite DB during the migration.

## 3. Delivery roadmap

### Phase 0 — Baseline and change control

**Purpose:** Make every subsequent result reproducible.

1. Create a `policy_version` and `policy_hash` for every signal, account metric, event, replay run, and EA startup.
2. Add a deployment manifest: Python commit, EA source/build identifier, schema version, broker/account scope, symbol mapping, policy hash, and data-set manifest.
3. Define environments: `unit`, `replay`, `mock`, `demo`, and `live`; prohibit live mode until an explicit future approval process exists.
4. Consolidate startup migrations into `run_all_migrations()` and record migration version/health.
5. Establish CI commands for formatting, Python tests, migration tests, static checks, EA compilation, and contract fixtures.

**Acceptance criteria**

- Every trade and decision can be traced to a policy, code revision, and data version.
- A fresh database and an upgraded database both migrate idempotently.
- The current 226-test baseline remains green before each change.

### Phase 1 — Non-negotiable execution safety

**Purpose:** Remove ways to breach the SRS controls after a signal has been generated.

#### 1A. Atomic portfolio reservations

1. Add `trade_reservations` (or a lifecycle-state extension) keyed by `trade_id`, with: proposed risk, symbol, currency-factor exposure, policy hash, expiry, state, and broker identifiers.
2. In one database transaction: obtain a current broker snapshot, include `OPEN`, `PENDING`, and `EXECUTION_UNCERTAIN` exposure, validate policy, then create the reservation.
3. Route only after the reservation succeeds. Release/transition it only on a terminal EA acknowledgement, verified broker reconciliation, or explicitly reviewed expiry.
4. Replace `INSERT OR REPLACE` for trade proposals with append-only decision events and explicit, allowed state transitions.
5. Add a cycle-level candidate allocator so a six-symbol evaluation is ranked and allocated against a shared capacity, not independently approved from the same snapshot.

#### 1B. Policy parity and sealed execution envelope

1. Extend the signal contract with `schema_version`, `policy_hash`, `issued_at`, `expires_at`, `max_spread_pips`, `max_positions`, `reservation_id`, and policy limits needed by the EA.
2. The EA must reject an invalid/expired/envelope-policy mismatch before any order request.
3. Make EA spread validation enforce the stricter of its configured input and signal ceiling. Core SRS must never execute above 2.0 pips.
4. EA must count relevant open/pending positions before send and reject an unreserved/over-cap trade.
5. Preserve idempotency across Python restart, EA restart, file replay, and broker acknowledgement delay.

#### 1C. Broker-aware sizing and execution validation

1. Create one canonical sizing interface returning proposed lot, quantized lot, expected loss at SL, margin, cost envelope, and rejection reason.
2. Use broker contract data / `OrderCalcProfit` and `OrderCalcMargin` where available; avoid hard-coded contract-size assumptions for live sizing.
3. In the EA validate direction, symbol trade mode, exact lot grid precision, price side, margin, stop distance, freeze distance, and final loss at SL.
4. Include requested and resolved entry/SL/TP/volume, retcode, broker reason, spread in pips, and realized sizing in feedback.

**Phase 1 acceptance tests**

- Six simultaneous valid candidates reserve/route no more than two positions and no more than 5% combined risk.
- A stale, invalid, policy-mismatched, duplicate, or unreserved signal never reaches `Buy`, `Sell`, `BuyLimit`, or `SellLimit`.
- A 2.01-pip core-SRS fill is rejected by EA policy.
- A minimum-lot/cost/slippage scenario whose worst loss exceeds reservation is rejected.
- Restart during each lifecycle point gives one terminal outcome only—no duplicate fill and no orphan reservation.

### Phase 2 — Data integrity and market-operation controls

**Purpose:** Ensure a signal is based on usable, current information.

1. Introduce `DataQualityDecision` and block a cycle when any required input fails.
2. Require only completed M15/H1/H4 bars for decision features. Record bar open/close times and the decision time.
3. Make account snapshot, quote, OHLC, calendar, and symbol-contract maximum ages explicit policy values.
4. Replace static calendar handling with a sourced updater or controlled import workflow containing source, fetch time, coverage end, checksum, and high-impact taxonomy. Fail closed for absent/malformed/expired calendar data.
5. Give rate differentials a source and expiry; when stale, record a degraded-mode event and disable only the soft modifier if that is the approved policy.
6. Convert session handling to named zones and test DST, holidays, Sunday open, Friday close, daily rollover, and broker server-time differences.
7. Add a broker symbol resolver (`EURUSD` -> broker-specific symbol) and validate all configured instruments at startup.

**Acceptance criteria**

- Any expired required calendar, quote, snapshot, or bar data produces a persistent no-trade decision.
- DST/session fixture tests pass for both London and New York transition weeks.
- Every decision identifies exact completed source bars and input ages.

### Phase 3 — Production-faithful replay and research platform

**Purpose:** Measure the actual system hypothesis instead of a simplified single-pair proxy.

1. Extract current agents into pure interfaces usable by both live orchestration and replay.
2. Build a chronological, multi-symbol event replay that supplies completed bars and next-available bid/ask fills.
3. Replay the shared reservation, hard-risk, sizing, order-state, partial-close, BE, trailing, and recovery rules—not a second strategy implementation.
4. Use broker-consistent M15 bid/ask, variable spread, commission, swap, and realistic slippage; version every data set and prevent train/test leakage.
5. Add fault-injection scenarios: rejected orders, delayed files, missing feedback, stale snapshots, EA restarts, weekend gaps, and partial fills.
6. Produce per-run artifacts: configuration, data manifest, all decisions, order lifecycle, equity curve, risk events, and a machine-readable metrics file.

**Required reports**

- Portfolio and per-pair trade count, expectancy, average/median R, win rate, payoff ratio, profit factor, drawdown, time-to-recovery, and risk breaches.
- Per-regime, direction, session, spread bucket, and stop/management-mode attribution.
- Gross versus net results, median/p95 costs, and sensitivity under base/adverse stress costs.
- A reconciliation report proving no order/lifecycle event is unaccounted for.

### Phase 4 — Strategy optimization under controls

**Purpose:** Discover whether an edge persists after costs and controls.

Optimize by one hypothesis at a time; avoid a broad parameter sweep across the entire system.

1. **Entry validity:** Compare the active 20-pip / unconstrained-RSI policy against documented constrained alternatives. Treat this as an ablation, not a presumed improvement.
2. **Trend regime:** Test ADX, EMA-distance normalized by ATR, H1/H4 alignment, and volatility filters.
3. **Stop and target geometry:** Compare pure ATR, structural snap, and hybrid stops; measure realized loss versus intended risk.
4. **Management:** Evaluate fixed target, BE only, partial + fixed, trail, and hybrid on identical signals with costs included.
5. **Portfolio construction:** Replace the five-day absolute-level correlation measure with return-based, direction-aware currency-factor exposure and evaluate the added selectivity cost.
6. **Pair eligibility:** Retain/disable pairs only from sufficiently sized, out-of-sample evidence; do not infer from pooled results.

**Experiment protocol**

- Freeze a baseline policy hash and test one declared change per experiment.
- Use purged/embargoed rolling walk-forward splits plus a final untouched holdout period.
- Require a minimum trade count per comparison and report confidence intervals/uncertainty rather than a single point metric.
- Promote only when net out-of-sample improvement persists under adverse cost scenarios and does not weaken risk behavior.

### Phase 5 — Intelligence/ML promotion (research only until eligible)

1. Keep the ranker/meta-labeler in shadow mode until a data-quality review confirms correct labels, no future leakage, stable feature definitions, and enough closed trades by relevant regime/pair.
2. Train only from immutable, versioned trade events; use time-based splits and probability calibration.
3. Define a promotion ladder: disabled -> shadow -> soft monitoring -> bounded canary -> approved gate.
4. A model must be able to abstain, log its feature/vector version, and fail open to the deterministic baseline only if that is the explicitly approved safety policy.

**Promotion criteria:** calibrated probabilities, out-of-sample improvement net of costs, no concentration in a single period/pair, and a rollback tested in demo.

### Phase 6 — Frozen forward demo and release decision

1. Freeze code, policy hash, EA build, data-quality rules, and broker configuration.
2. Run for at least 30 calendar days and 25 closed trades; do not tune during the run.
3. Use SRS benchmarks (>=45% win rate, >=2.0 average R, <=15% drawdown) as minimum screens—not proof of profitability.
4. Add operational gates: zero hard-risk breaches, zero stale-data bypasses, zero unexplained lifecycle events, 100% restart reconciliation, and documented broker-cost comparison.

## 4. Regression specification

Every change must add automated tests at the boundary it affects.

| Concern | Required regression cases |
|---|---|
| Risk reservation | Concurrent candidate allocation; duplicate idempotency; expiry; broker/DB mismatch; pending counts toward limits. |
| EA contract | Schema compatibility, malformed/escaped payloads, TTL, policy hash, max-spread parity, symbol and direction checks. |
| Sizing | Different quote currencies, JPY/non-JPY, fractional lot steps, min/max lot, commission/slippage, stops/freeze, margin. |
| Lifecycle | Route -> reject/fill -> partial -> final exit; restart at every stage; feedback retry/deduplication. |
| Data quality | Stale/missing calendar, OHLC, quote, snapshot; incomplete current candle; DST/holiday/rollover. |
| Backtest/replay | No look-ahead; next-quote fill; multi-pair capacity; adverse costs; out-of-order and missing events. |
| Database | Fresh/upgrade migrations, append-only decisions, invalid state transitions, partition/evidence queries. |

## 5. Prioritization and dependencies

| Priority | Workstream | Depends on | Enables |
|---|---|---|---|
| P0 | Phase 0 baseline/change control | None | Reproducible work. |
| P0 | Phase 1 reservations and EA policy parity | Phase 0 policy identity | Safe demo operation. |
| P0 | Phase 2 freshness controls | Phase 0 policy identity | Trustworthy signals. |
| P1 | Canonical sizing/EA broker rules | Phase 1 envelope | Accurate risk and cost. |
| P1 | Shared replay platform | Phases 1–2 interfaces | Valid optimization evidence. |
| P2 | Strategy experiments | Shared replay platform | Frozen candidate policy. |
| P3 | ML promotion | Immutable events + validated baseline | Bounded incremental selectivity. |
| P3 | Forward demo | All prior acceptance gates | Release decision. |

## 6. Implementation sequence for the next development cycle

1. Write failing tests for same-cycle over-allocation, stale/expired signal rejection, and 2-pip EA parity.
2. Design/implement the reservation state machine and transaction boundaries in SQLite.
3. Version the policy/signal envelope and make EA validate it; compile EA and exercise the contract fixtures.
4. Add data freshness guards and replace current stale calendar/rate files with a controlled source/update workflow.
5. Extract the orchestration responsibilities from `main.py` only after the Phase 1 contracts are protected by tests.
6. Implement the shared replay adapter and establish the first realistic, multi-pair baseline before changing any strategy parameter.

## 7. Decisions still needed from the owner

These are product/risk decisions, not implementation details:

1. The broker/account(s), account base currency, leverage, symbol suffixes, and whether netting or hedging mode is used.
2. The authoritative source and update ownership for the economic calendar and broker cost data.
3. The maximum signal TTL and how an unacknowledged order is resolved operationally.
4. Whether the SRS 3.2% base risk remains the intended demo policy after safety fixes; do not change it silently during the audit program.
5. The historical data source and duration available for realistic replay.

## 8. Immediate-cycle completion record

Implemented on 2026-08-27:

- Transactional pending-risk reservations with position and combined-exposure enforcement.
- Versioned, expiring policy envelopes and Python/EA validation.
- Strict 2-pip core spread parity and EA position-cap recheck.
- Completed-bar MT5 reads, fail-closed calendar coverage, and stale-rate degradation.
- Application routing service extracted from the engine orchestration.
- Append-only lifecycle events with identity-preserving trade upserts.
- Chronological multi-symbol replay and explicit spread/slippage/commission cost interfaces.
- Controlled calendar/rate import validation.
- Regression baseline: 235 Python tests passing.

MT5 compilation and demo execution remain external acceptance activities because MetaEditor/MT5 is not installed in this workspace. A performance baseline also remains dependent on obtaining broker-consistent multi-symbol historical data; no performance result should be fabricated from synthetic data.
