# ADR 0007: Continuous demo testing instead of a fixed 30-day demo

Date: 2026-11-04. Status: accepted by the owner for development. The SRS change it implies is still to be written (see "Open").

## Context

The plan (`fx_ai_engine/docs/plans/2026-11-03-full-development-and-ai-plan.md`) puts a frozen 30-day forward demo (Phase 6, gate G6) after strategy evidence (G2) and SRS v1.1 (G3). The draft SRS v1.1 proposal (`fx_ai_engine/docs/2026-11-04-srs-v1.1-draft-proposal.md`) left the choice open as decision G2-START.

The owner's direction on 2026-11-04: no 30-day demo. The system has been developed while it is tested, and it will keep improving that way. Testing on the demo account is how the system is implemented and improved, not a separate frozen phase at the end.

## Decision

1. Plan Phase 6 and gate G6 are dropped from the development path. No frozen 30-day demo run is required before or during development.
2. Development and demo testing alternate in test waves the owner starts (the operating rule in `docs/STATUS.md`: the engine and long jobs do not run unattended unless the owner starts a test wave).
3. Each test wave runs one **release**: a git commit plus a freeze manifest made at the start with `research.forward_test.freeze create`. Any change to code, config, policy or the EA starts a new release with a new manifest. Results are kept per release and are not pooled across releases unless the policy hash is the same.
4. The SRS v1 §12.1 no-change rule applies within a release only.
5. Each release is read with the existing tools, report-only:
   - `research.forward_test.freeze check`: confirms the running release matches its manifest.
   - `research.forward_test.evaluate`: evaluates the release's closed trades. Use criteria written for that release, with no 30-trading-day minimum. A minimum closed-trade count still applies, and below it the verdict stays INCONCLUSIVE.
   - R7 `research.fill_reconciliation`, R8 `research.drift_monitor` and R9 `research.trade_postmortem`: fills against the replay cost model, drift against the replay distribution, and the cause of each closed trade.
6. Strategy changes meant to make the system profitable are tested in replay first (R1/R2 authenticity gate, R4 cost stress, R3 multiple-testing correction) and then on the demo as a new release. A demo result alone is not evidence of edge; the per-release sample is too small (proposal §2.3: about 118 trades for 80% power at +0.25R).

## What does not change

- The AGENTS.md invariants: only the EA places orders, the Hard Risk Engine has the final say, AI is shadow-only.
- The SRS v1 locked constants (3.2% risk per trade, 2 open trades, 5% combined exposure, 8% / 15% / 20% stops, 3-loss halt, R:R 2.2). Lowering risk during demo testing (D3) still needs an explicit owner instruction and an SRS revision.
- Demo account only. Real money stays a separate owner decision.
- Phase 2 result: every tested configuration of the current strategy loses about 0.32–0.36R per trade after costs in replay (ADR 0006, `docs/STATUS.md`). Demo testing of the current strategy therefore measures execution and costs, not profit. Profitability depends on the strategy redesign (plan Phase 2b).

## Open

- **Pre-live gate.** `SRS_v1.md` §12 says "No live deployment before successful 30-day demo", and §11 item 8 lists the 30-day demo as a build step. This ADR covers development only. Whether that gate still applies before real money, or what replaces it, has to be decided and written into SRS v1.1 (version bump, owner approval).
- Decisions D2, D3 and O1–O5 in the draft SRS v1.1 proposal are still open. G2-START is answered by this ADR.
- The plan's definition of v1 complete (§1 item 4) refers to the 30-day demo and needs revising with SRS v1.1.
- `fx_ai_engine/docs/research/forward-test/criteria-srs-v1.1-draft.json` keeps its 30-trading-day minimum because the tests use it as a fixture. The criteria for a real release are written when that release's test wave starts.
