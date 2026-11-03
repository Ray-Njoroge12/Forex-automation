# System build roadmap

Date: 2026-11-02. This roadmap translates the current repository findings into independently reviewable builds. It supplements the SRS and earlier plans; it does not certify trading readiness. Status reflects the source review, not a complete re-audit. Each build should produce code, relevant tests, and recorded evidence of its acceptance criteria.

## Current delivery status

- Build 1: atomic execution-feedback application, conflict recording and fail-closed uncertainty implemented in Python; MT5 execution evidence remains unverified.
- Build 2: expired and orphan reservations block entries, MT5 position-query failures are not treated as an empty account, and a read-only reconciliation inventory is available. The EA source writes a pre-submission marker for restart recovery; this assumes one EA per bridge and requires terminal validation. Broker-history correlation and a verified resolution workflow remain outstanding.
- Build 3: EA source checks server return code, deal, price and volume before reporting a market fill; other outcomes remain uncertain. These EA changes have not been compiled or tested in MT5, and incomplete fills and pending orders need broker-specific recovery. MetaQuotes specifies that a successful CTrade method return does not itself prove execution: https://www.mql5.com/en/docs/standardlibrary/tradeclasses/ctrade/ctradebuy. Server return codes are documented at https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes.
- Build 4: separate quote-bar execution primitives now cover bid/ask entry, adverse slippage, gap-through stops, and ambiguous stop/target ordering. They are not integrated with portfolio or strategy replay and do not establish production parity.
- The existing Backtrader strategy resamples available input to H1/H4 without explicitly excluding a partially formed final aggregation. Define the input timestamp convention and completed-bar cutoff before treating its multi-timeframe results as evidence; tests must cover gaps and boundary timestamps.
- Builds 4–7 remain incomplete. Historical data, fresh market context, integrated replay, independent baseline, demo evidence, and strategy evaluation remain required.

## Existing foundation

The system already has Python decision and risk components, an MT5 file bridge and EA, a reservation ledger, feedback receipts, snapshot reconciliation, market-data freshness checks, a replay event scaffold, dataset manifests, and a demo test matrix. These are starting points, not evidence that every production scenario is covered. The Python suite most recently passed 251 tests. EA compilation, demo validation and a reproducible performance baseline remain unverified.

## Build 1 — Execution feedback integrity (P0; first)

**Build:** Define the supported feedback contract and explicit reservation/order state transitions. Apply feedback, lifecycle event, reservation change and deduplication receipt in one SQLite transaction. Treat ambiguous broker outcomes and contradictory, late or malformed messages as unresolved; retain risk capacity and surface a reason. Delete source artifacts only after commit.

**Existing areas:** `core/schemas.py`, `bridge/execution_feedback.py`, `database/db.py`, `main.py`.

**Acceptance:** duplicate delivery has one effect; simulated failure at every transactional boundary leaves no partial committed outcome; invalid or contradictory evidence cannot silently release risk; transitions and unresolved cases are auditable. Confirm the meaning of MT5 `REJECTED_ORDER_SEND` and broker retcodes before classifying it as definitive.

## Build 2 — Broker reconciliation and restart recovery (P0)

**Build:** Correlate each local trade and reservation with broker orders, deals and positions using stable identifiers and account scope. Compare sufficiently fresh snapshots and history, and define conservative handling for incomplete, delayed or conflicting evidence. Provide an operator-visible list and documented evidence-based resolution process for stalled reservations. Preserve exposure while uncertain.

**Existing areas:** `core/state_sync.py`, `core/mt5_bridge.py`, `main.py`, `database/db.py`, bridge artifacts.

**Acceptance:** restart or interruption before/after publication, execution and feedback neither duplicates orders nor silently releases exposure. Missing or stale evidence blocks affected risk allocation. Matching and resolution decisions are reproducible from retained evidence. Depends on Build 1's state semantics.

## Build 3 — MT5 execution and broker contract (P0/P1)

**Build:** Audit and harden EA order submission against actual broker rules: volume step and limits, price precision, trade mode, stop/freeze distances, margin, fill modes, final spread, order retcodes and post-fill protection. Align sizing and cost assumptions across Python, EA and replay; record request, broker response, fill and resulting stop/volume. Assess EA31337's pinned framework submodules as reference, without copying code absent license review.

**Existing areas:** `mt5_ea/FX_Execution.mq5`, `core/mt5_bridge.py`, risk and sizing code, `validation/ea_demo_matrix.py`.

**Acceptance:** contract tests cover broker-rule boundaries and rejected or uncertain sends; EA compiles in the intended MT5 terminal; demo matrix results and logs reconcile with Python records. Actual broker and terminal access are external prerequisites. Coordinate with Builds 1–2; complete before demo-readiness sign-off.

## Build 4 — Replay and portfolio simulation (P1)

**Build:** Define time semantics for completed bars and orders, then build deterministic chronological multi-symbol replay using shared decision and risk interfaces. Simulate bid/ask, spread, slippage, commissions, swaps where applicable, stop gaps, pending orders, position and reservation limits, and rejected/delayed execution. Explicitly represent intrabar ambiguity and unsupported behavior.

**Existing areas:** `backtesting/event_replay.py`, `backtesting/simulation_profile.py`, risk and routing interfaces, dataset manifest tools.

**Acceptance:** no future-bar access or fill before submission; reproducible identical runs; scenarios for gap-through-stop, adverse costs, multi-symbol exposure, interruptions and reconciliation; traceable decisions, orders, fills, positions and equity. Depends on stable lifecycle and risk semantics from Builds 1–3; portions of simulator design can proceed earlier.

## Build 5 — Data and research evidence (P1)

**Build:** Source and document reviewed economic-calendar and rate data; install via the existing importer. Acquire broker-consistent historical bid/ask or the best available data with explicit limitations. Validate time zones, gaps, spread coverage and data provenance; generate immutable manifests and frozen configuration for each experiment. Produce cost-sensitive walk-forward and holdout reports.

**Acceptance:** strict preflight passes with current approved inputs; every replay identifies input hashes, coverage, policy, model assumptions and limitations; baseline can be independently reproduced. Strategy promotion requires a separately retained holdout and adverse-cost assessment. Current calendar and rate inputs failed freshness preflight. Data acquisition can proceed alongside Builds 1–4; credible baseline requires Build 4.

## Build 6 — Operations and readiness (P1)

**Build:** Surface engine/bridge health, stale inputs, unresolved reservations, broker mismatches, policy identity and halt reasons through existing dashboard and alerts. Provide startup, shutdown, incident recovery and demo runbooks. Produce a readiness report linking test results, EA build, broker configuration and validation evidence.

**Acceptance:** operators can identify why entries are blocked, inspect unresolved risk and follow recovery steps without editing database records blindly. Readiness cannot pass when required evidence is absent. Integrate with Builds 1–3 and 5.

## Build 7 — Strategy evaluation and controlled changes (P2)

**Build:** Inventory the active policy and parameter provenance. Evaluate individual strategy and risk-management hypotheses using frozen replay baselines, cost stress tests and walk-forward partitions. Keep ML in research/shadow mode until separate data, lineage and validation requirements are met.

**Acceptance:** every proposed parameter or model change has a documented hypothesis, frozen inputs, out-of-sample comparison and rollback; no claim of performance is based on the current smoke simulation. Depends on Builds 4–5 and verified operational controls.

## Gates and order

1. **Safety foundation:** Builds 1 and 2, with broker-contract work from Build 3. Begin data acquisition in parallel.
2. **Demo evidence:** complete Build 3, current market inputs and the relevant operator controls in Build 6. Run the acceptance matrix on a disposable demo account.
3. **Research evidence:** complete Builds 4 and 5 and establish an independently rerunnable baseline.
4. **Controlled strategy development:** Build 7 only after the evidence gates above. Any later live-capital decision requires a separate review of the SRS validation protocol and actual demo results.

## First implementation slice

Start Build 1 by documenting the current feedback/reservation transition table, including what evidence makes each transition valid. Add meaningful tests for duplicate and contradictory feedback and transactional interruption. Implement atomic persistence and conservative ambiguity handling, then run the Python suite. Do not assume a failed order-send response proves that no broker execution occurred without examining MT5 behavior and retcodes.
