# Reference repository analysis and development priorities

Date: 2026-11-02. Scope: targeted source review, not an exhaustive audit or an endorsement of trading performance. Revisions below make the observations reproducible. GitHub CLI was used read-only. No external code was copied.

## Reference observations

### NautilusTrader: reconciliation as ordered evidence

- Revision: [40acca43c47cb7e0c7fdde964e9395a26df17a62](https://github.com/nautechsystems/nautilus_trader/tree/40acca43c47cb7e0c7fdde964e9395a26df17a62).
- Its [order reconciliation](https://github.com/nautechsystems/nautilus_trader/blob/40acca43c47cb7e0c7fdde964e9395a26df17a62/crates/execution/src/reconciliation/orders.rs) turns venue reports into ordered events. It distinguishes authoritative snapshots from ordinary reports when deciding whether a lower cumulative fill warrants a correction. It notes that the caller must establish snapshot freshness.
- The [reconciliation tests](https://github.com/nautechsystems/nautilus_trader/blob/40acca43c47cb7e0c7fdde964e9395a26df17a62/crates/execution/src/reconciliation/tests.rs) cover partial lifecycle evidence, inferred fills and external order states. These tests are examples of edge cases, not proof that our MT5 adapter behaves identically.
- Implication: distinguish broker evidence, local projections and inferred events; require explicit evidence and ordered, idempotent transitions. Do not infer that a missing position alone proves rejection.

### LEAN: explicit fill assumptions

- Revision: [3b2bf19e597805cc917dd199ed9ee7d10d5ceb84](https://github.com/QuantConnect/Lean/tree/3b2bf19e597805cc917dd199ed9ee7d10d5ceb84).
- Its [fill model](https://github.com/QuantConnect/Lean/blob/3b2bf19e597805cc917dd199ed9ee7d10d5ceb84/Common/Orders/Fills/FillModel.cs) checks order timing and market hours and uses a slippage model. Its stop-market logic refuses stale bars and can fill worse than the stop price; it assumes a complete fill. This is one model, not a universal broker specification.
- Implication: define our bid/ask, timestamps, slippage, gap, market closure and partial-fill assumptions explicitly, and evaluate adverse cases. OHLC bars do not reveal the ordering of intrabar events.

### EA31337: MT5-specific structure

- Revision: [9b9bd38c4e49dd6916f4fe3936e50e97fca7a692](https://github.com/EA31337/EA31337/tree/9b9bd38c4e49dd6916f4fe3936e50e97fca7a692).
- The [MT5 entry point](https://github.com/EA31337/EA31337/blob/9b9bd38c4e49dd6916f4fe3936e50e97fca7a692/src/EA31337.mq5) delegates to included classes and initializes trading and spread parameters; substantial functionality lives in [submodules](https://github.com/EA31337/EA31337/blob/9b9bd38c4e49dd6916f4fe3936e50e97fca7a692/.gitmodules). The entry point alone is insufficient to assess order handling or broker safety. Further examination must follow the exact submodule revisions.
- Implication: inspect actual broker validation and trade transaction handling before borrowing any design claims. This project is GPL-3.0; review applicable licensing before reuse.

## Current-system gap matrix

| Priority | Evidence in this project | Risk / next action |
| --- | --- | --- |
| P0 | `core/schemas.py::validate_execution_feedback` now rejects unknown statuses and executions without a positive integer ticket. Invalid files are quarantined before feedback application. `main.py::_consume_feedback` still maps validated non-execution statuses to reservation `REJECTED`. | The initial unknown-status and invalid-ticket gap is guarded. Further work must validate other execution values and confirm that each rejection, especially order-send failure, is sufficient evidence of non-execution. Test duplicate, delayed and contradictory feedback and retain uncertainty where evidence is inconclusive. |
| P0 | Feedback trade update, reservation transition, and receipt are separate DB operations in `main.py::_consume_feedback`. | A crash between operations can leave a partial projection or duplicate lifecycle events. Design one transaction for the trade projection, reservation transition and receipt, with idempotent retry; delete artifacts only after commit. Include fault-injection tests. |
| P0 | `database/db.py` transition functions accept any supported destination without checking the previous state. | Late or contradictory feedback may overwrite terminal reservation state. Specify allowed transitions, evidence and duplicate behavior; test out-of-order delivery and restart recovery. |
| P1 | `main.py` consumes feedback then refreshes account state. `core/state_sync.py` compares the broker snapshot with the local open ledger and blocks mismatches. | This is an existing protective control. Test the full feedback/snapshot timing window, especially an older snapshot after a fill, same-count mismatches and delayed history. Do not assume that timestamp freshness alone proves a snapshot includes the fill. |
| P1 | `backtesting/event_replay.py` supplies completed-bar events and a constant spread/slippage helper, but not a complete portfolio, order lifecycle or broker simulator. | Build a shared decision/risk replay with next-observation execution, bid/ask and explicit cost models. Add stop gap, intrabar ambiguity, delayed/rejected orders and portfolio reservation scenarios. Record unsupported behaviors in each report. |
| P1 | EA source and demo matrix exist, but Python source assertions do not run MQL5 or prove actual broker behavior. | Compile in the intended terminal and retain demo evidence for invalid/duplicate/expired signals, spread, capacity, broker rejections and restarts. Confirm broker volume step, stop/freeze rules and fills. |
| P1 | Current preflight reports stale calendar and rate inputs. | Obtain reviewed, licensed, current data and install with the documented importer. Do not bypass freshness gates. |

## Delivery sequence and acceptance

1. **Feedback and lifecycle integrity:** write failing tests for invalid/contradictory evidence and crash boundaries; implement a validated state machine and atomic idempotent feedback application. Acceptance: ambiguous evidence cannot release risk; retries do not duplicate effects; terminal conflicts remain visible and block new entries when exposure is uncertain.
2. **Reconciliation and recovery:** identify reservations by stable trade and broker identifiers, compare fresh broker order/deal/position history, and expose unresolved cases for operator review. Acceptance: crash at each publication/feedback stage cannot create duplicate orders or silently free exposure; reconciliation is conservative where history is incomplete.
3. **Replay parity:** freeze event timing and price-side semantics, implement shared live decision/risk interfaces and a deterministic multi-symbol portfolio/execution simulator. Acceptance: no future data, no same-bar fill before submission, gap and adverse-cost scenarios, and explainable reconciliation of decisions, orders, fills and positions.
4. **External evidence:** refresh sourced market context; compile and test EA against a disposable demo account; collect broker-specific cost and contract information; prepare hash-manifested historical data. No strategy parameter promotion until out-of-sample and adverse-cost results are reproducible.

These priorities are engineering hypotheses and acceptance criteria, not a claim that the system is ready for live capital or that any strategy has an edge.
