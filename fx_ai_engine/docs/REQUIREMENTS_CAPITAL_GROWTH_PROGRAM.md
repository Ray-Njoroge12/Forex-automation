# FX AI Engine Requirements - $10 Capital Growth Program

Date: 2026-04-10
Status: Baseline v1.0 (assumption-driven, implementation-ready)
Scope: Requirements and delivery blueprint for a phase-based capital growth program starting from assumed capital of $10, implemented on the current FX AI Engine architecture.

Default decision policy:
If an item below was previously open, this document now applies a concrete baseline default so implementation can proceed immediately. Future user direction may override these defaults explicitly.

## 1) Objective and Intent

The system objective for this program is:

1. Preserve a small starting capital assumption ($10) first.
2. Grow position size only as equity milestones are achieved.
3. Keep risk constraints strict and fail-closed at all times.
4. Use demo-account evidence before any real-capital decisions.

Important note:
"Assured wins" is not technically achievable in real markets. This document treats that goal as:

- maximizing decision quality,
- minimizing avoidable losses,
- enforcing strict risk discipline,
- and improving expectancy over time.

## 2) Source-of-Truth Constraints

This program must remain consistent with the system architecture and SRS v1 guardrails already present in the project.

### 2.1 Locked SRS v1 Constraints (Non-Negotiable)

1. Risk per trade: 3.2% (core SRS mode)
2. Max open trades: 2
3. Max combined exposure: 5%
4. Daily stop loss: 8%
5. Weekly stop loss: 15%
6. Drawdown halt: 20%
7. Consecutive loss halt: 3
8. Minimum R:R: 2.2
9. Instruments: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF
10. Timeframes: H1 (regime), M15 (execution)

### 2.2 Architecture Boundaries (Must Stay True)

1. Decision chain remains fixed:
   Regime -> Technical -> Adversarial -> Portfolio -> Hard Risk Engine -> Router -> MT5 EA -> Feedback -> SQLite
2. Hard Risk Engine remains the final authority and cannot be bypassed.
3. MT5 EA remains the only component that places broker-side orders.
4. Signal routing remains atomic (tmp -> rename).
5. All approvals, rejections, and execution outcomes remain auditable in SQLite.

## 3) Program Scope

### 3.1 In Scope

1. Requirements for phased position-size growth from an assumed $10 baseline.
2. Requirement definitions for risk, execution safety, and evidence collection.
3. Development approach and sequencing for implementation.
4. Demo testing strategy accounting for the broker demo account scale mismatch.
5. Documentation for acceptance gates and stop conditions.

### 3.2 Out of Scope (for this document)

1. Actual code changes in this step.
2. Direct strategy parameter optimization.
3. Any removal or relaxation of SRS hard limits.
4. Any claim of guaranteed profitability.

## 4) Capital Growth Program Specification

## 4.1 Capital Model Definitions

1. Assumed Starting Capital (ASC): $10
2. Real Demo Equity (RDE): actual broker demo account equity (currently simulated around $100,000)
3. Shadow Equity (SE): normalized equity used for $10-based analysis and stage progression
4. Phase: a predefined risk profile unlocked only when milestone criteria are met

## 4.2 Phase Logic (Adopted Baseline)

The current repository already contains milestone values in policy logic. This document adopts that mapping as the baseline for implementation.

Proposed milestone sequence:

1. Phase P1: SE $10-$19.99, fixed risk $0.50, max trades 1
2. Phase P2: SE $20-$49.99, fixed risk $0.75, max trades 1
3. Phase P3: SE $50-$99.99, fixed risk $1.50, max trades 1
4. Phase P4: SE $100-$199.99, fixed risk $3.00, max trades 2
5. Phase P5: SE $200-$499.99, fixed risk $6.00, max trades 2
6. Phase P6: SE $500+, transition candidate to core SRS percentage regime (subject to explicit governance decision)

## 4.3 Phase Transition Rules

A phase transition should occur only when all conditions are true:

1. Equity milestone reached in the tracked capital model.
2. No active hard-risk halt state.
3. Last N-trade health gate passes (N is fixed to 20 for baseline).
4. Maximum drawdown for the current phase remains under configured threshold.
5. No unresolved execution reliability failures in the phase window.

Baseline drawdown ceilings by phase:

1. P1-P2: <=10%
2. P3-P4: <=12%
3. P5-P6: <=15%
4. Global abort floor remains 20% per locked SRS halt logic.

## 4.4 Fail-Closed Downgrade Rules

Automatic phase downgrade (or freeze) should trigger on:

1. hard drawdown breach,
2. repeated state reconciliation failures,
3. execution uncertainty events above threshold,
4. repeated pre-route infeasibility or broker lot rejection events.

## 5) Demo Account Reality and $10 Normalization

The current demo environment simulates approximately $100,000 equity, while this program assumes $10.
That mismatch must be handled explicitly to avoid false confidence.

## 5.1 Core Risk Distortion Problem

With a large standard demo account:

1. lot minimum effects are different,
2. margin burden is different,
3. spread and commission burden relative to account size is much smaller,
4. drawdown tolerance appears easier than in a true $10 context.

Therefore, raw demo results cannot be treated as direct proof of $10 viability.

## 5.2 Two-Track Validation Requirement

Track A: Execution Integrity Track (Real MT5 Demo)

1. Purpose: verify bridge, routing, fills, feedback loops, and operational stability.
2. Uses real demo account connectivity.
3. Primary outputs:
   - connectivity reliability,
   - spread sanity,
   - order lifecycle correctness,
   - reconciliation stability,
   - risk-event integrity.

Track B: Capital Fidelity Track (Shadow/Simulated $10)

1. Purpose: verify whether the strategy and phase logic are viable under micro-capital economics.
2. Uses normalized analytics and/or mock/backtest evidence where appropriate.
3. Primary outputs:
   - milestone progression realism,
   - drawdown behavior at $10 scale,
   - phase transition safety,
   - cost burden viability.

Both tracks are required before any confidence claim.

## 5.3 Shadow Equity Normalization (Proposed)

Given RDE around $100,000 and ASC=$10, define:

SE = RDE / NormalizationDivisor

Baseline divisor for pure equivalence:

NormalizationDivisor = 100000 / 10 = 10000

This gives:

SE(start) = 100000 / 10000 = 10

Important:
The current runtime has a preserve-10 virtual balance divisor concept and startup checks. Depending on broker account type (cent vs standard), runtime behavior may reject preserve-10 mode in live demo execution due economics/lot granularity constraints.

So the normalization policy for reporting can be stricter than what is executable at broker lot minimum.

Baseline normalization policy adopted:

1. Reporting and phase-progression analytics use strict normalized shadow equity (divisor 10000).
2. Runtime execution feasibility continues using broker-compatible checks and preserve/startup gates.
3. Any divergence between analytic normalization and executable broker constraints is logged as an explicit operational caveat.

## 5.4 Required Test Interpretation Policy

1. Track A pass does not imply Track B pass.
2. Track B pass does not imply live execution readiness.
3. Combined pass is required for claims about this $10 growth program.

## 6) Functional Requirements (Program-Specific)

### FR-01: Phase State Management

The system shall maintain explicit phase state with:

1. current phase id,
2. phase entry timestamp,
3. phase entry equity,
4. latest phase health decision,
5. promotion/demotion reasons.

### FR-02: Stage-Aware Risk Selection

For non-core research mode, the system shall apply phase-specific fixed-risk sizing and max trade count without bypassing hard risk constraints.

### FR-03: Promotion Gate

The system shall promote phase only after milestone and quality gates pass.

### FR-04: Downgrade/Freeze Gate

The system shall freeze or downgrade phase on risk-health breach conditions.

### FR-05: Evidence Logging

Each phase decision shall be persisted with reason codes in DB for audit.

### FR-06: Demo-Scale Normalized Metrics

The system shall produce normalized (shadow) performance metrics from demo evidence to support $10-based analysis.

### FR-07: Operational Health Gate

The system shall include operational blockers for:

1. stale snapshot evidence,
2. reconciliation failures,
3. execution uncertainty artifacts,
4. repeated lot feasibility failures.

### FR-08: Test Report Generation

Validation reports shall include both:

1. execution integrity metrics,
2. capital-fidelity metrics.

## 7) Non-Functional Requirements

### NFR-01 Determinism

All decision logic remains deterministic and reproducible.

### NFR-02 Reliability

Fail-safe behavior under any bridge, MT5, or state-sync uncertainty.

### NFR-03 Auditability

Every important decision and failure mode is timestamped and queryable.

### NFR-04 Governance

No silent runtime override of locked SRS core constraints.

### NFR-05 Security

Credentials remain environment-based and not committed.

### NFR-06 Performance

Decision cycle remains comfortably within M15 cadence.

## 8) Development Approach and Delivery Sequence

## 8.1 Delivery Principle

Given the test suite reset, development should be test-first from this point onward.

## 8.2 Recommended Delivery Waves

Wave 0: Documentation and Requirement Freeze

1. Lock this baseline requirements document as implementation source.
2. Use adopted phase milestones and normalization method from Sections 4 and 5.
3. Use adopted transition and drawdown gates from Section 4.3.

Wave 1: Test Foundation Rebuild

1. Recreate minimal test harness (unit + integration skeleton).
2. Add tests for phase-state transitions.
3. Add tests for hard risk non-bypass invariants.
4. Add tests for normalization/reporting correctness.

Wave 2: Phase Manager Implementation

1. Implement explicit phase state object and persistence.
2. Implement promotion/demotion evaluator.
3. Add reason-code event logging.

Wave 3: Dual-Track Validation Pipeline

1. Implement execution-integrity report path.
2. Implement capital-fidelity (shadow) report path.
3. Ensure reports are generated from same evidence period.

Wave 4: Demo Program Trial

1. Run time-boxed demo cycle under locked settings.
2. Publish weekly evidence summary.
3. Enforce no-tuning window during measured run.

## 8.3 Mandatory Quality Gates Per Wave

1. Unit tests pass.
2. Integration tests pass.
3. Smoke run passes.
4. No new unresolved critical risk events.

## 9) Demo Testing Specification

## 9.1 Entry Criteria

1. MT5 connectivity verified.
2. Bridge path and feedback path verified.
3. Symbol spread checks within policy threshold.
4. Risk-event logging operational.

## 9.2 Daily Test Checklist

1. Run health checks and bridge diagnostics.
2. Record execution failures/retries.
3. Record rejected-trade reasons distribution.
4. Record shadow metrics (SE, drawdown, phase status).
5. Record hard-risk events and halts.

## 9.3 Weekly Review Checklist

1. Win rate vs expectation envelope.
2. Average R vs threshold.
3. Drawdown path and max drawdown.
4. Phase progression stability.
5. Any divergence between Track A and Track B.

## 9.4 Program Abort Conditions

Abort or freeze progression when any is true:

1. drawdown breaches configured hard floor,
2. risk engine fails to halt when required,
3. reconciliation failures repeat above tolerance,
4. execution pipeline integrity is degraded,
5. measured expectancy collapses below floor for the active window.

## 10) KPI and Evidence Requirements

Required KPI set:

1. closed trades count,
2. win rate,
3. average R,
4. max drawdown,
5. pre-route rejection count,
6. late broker rejection count,
7. stale/reconciliation failure counts,
8. halted runtime sample count,
9. phase transitions count,
10. shadow-equity growth curve.

All KPIs must be persisted and reportable by evidence stream and account scope.

## 11) Key Risks and Mitigations

Risk 1: Standard demo account does not replicate micro-capital economics.

Mitigation:

1. dual-track validation,
2. normalized shadow metrics,
3. preserve-mode gating,
4. explicit disclosure when execution environment is non-equivalent.

Risk 2: Min-lot granularity creates infeasible risk at true $10 scale.

Mitigation:

1. pre-route feasibility checks,
2. strategic risk eligibility checks,
3. hard rejection of infeasible trades,
4. track infeasible-rate as primary KPI.

Risk 3: Operational reliability issues can invalidate trading conclusions.

Mitigation:

1. fail-closed on uncertainty,
2. reconciliation and stale-state blockers,
3. strict incident logging and review.

## 12) Applied Decision Register (Autonomous Defaults)

The following defaults are now adopted to remove ambiguity and unblock execution:

1. Phase progression basis:
   Use normalized shadow equity as primary progression source; use real demo equity for operational integrity checks.

2. Normalization divisor:
   Use strict equivalence divisor 10000 for reporting and phase analytics.

3. Milestone table:
   Keep the existing baseline sequence ($10/$20/$50/$100/$200/$500).

4. Promotion strictness:
   Require both milestone reach and minimum 20 closed trades in the active phase window.

5. Drawdown governance:
   Use tiered phase ceilings (10/12/15%) with global 20% abort floor.

6. Governance path:
   Run in preserve/research governance until P6 and stability gates are met, then require explicit migration decision before core_srs transition.

7. Demo account assumption:
   Assume standard demo denomination by default unless explicitly verified as cent account.

8. "Assured wins" operationalization:
   Treat as quantitative discipline targets: rolling win-rate >=45%, avg R >=2.0, max losing streak <=3.

9. Reporting cadence:
   Require both daily operational reports and weekly strategy-health reviews.

10. Demo exit criteria:
   Keep SRS 30-day gate unchanged and add phase-stability gate (no unresolved operational critical events in the final validation window).

## 13) Immediate Next Documentation Artifacts

The next required docs following this baseline are:

1. Capital Growth Phase Specification v1.0
2. Data dictionary for phase-state and shadow metrics
3. Test Plan v1.0 tied directly to the functional requirements above
