# Handoff 2026-11-04: validation waves 1–4 merged into hardening/2026-11

For the session that owns `dev/development-wave` (worktree `.worktrees/development-wave`). Read this before merging that branch into `hardening/2026-11` and before updating `docs/STATUS.md`.

## What landed on hardening/2026-11

- Validation waves 1–4 (research tooling, offline and report-only). Every file is new except the ADR renumbering and doc notes listed below. No protected path, `main.py`, `database/**`, `core/**` or `backtesting/**` file was changed.
  - Wave 1: `research/{bias_audit,authenticity_gate,cost_stress,monte_carlo,common}.py`, hypothesis cards, 5 skills, ADR 0002. This is the same content `dev/development-wave` cherry-picked.
  - Wave 2: `research/multiple_testing.py` (R3: deflated Sharpe, CSCV PBO, minimum 307 dev trades), skill `fx-multiple-testing`, **ADR 0006**.
  - Wave 3: `research/{fill_reconciliation,drift_monitor,trade_postmortem,demo_records,synthetic_demo}.py`, `research/live_vs_replay_thresholds.json`, 3 skills, **ADR 0004**.
  - Wave 4: `research/forward_test/{freeze,prereg,evaluate}.py`, draft criteria file, `fx_ai_engine/docs/2026-11-04-srs-v1.1-draft-proposal.md`, skill `fx-forward-test-protocol`, **ADR 0005**.
- **ADR 0007** (owner decision): no 30-day demo. Development and demo testing alternate in owner-started test waves, with one frozen release per wave. Plan Phase 6 and the proposal's G2-START carry a pointer to it.
- Test suite on hardening after the merge: 486 passed.

## ADR numbers

`dev/development-wave` owns ADR 0003 (`0003-development-wave-integration.md`). The waves now use 0004 (wave 3), 0005 (wave 4), 0006 (wave 2, first written as 0003) and 0007 (continuous demo testing). There is no filename collision. In STATUS.md's Decisions row, add 0004–0007.

## Expected merge conflicts with dev/development-wave

`git merge-tree` reports three add/add conflicts, all in docs. In each, dev's version is the newer text, because dev applied the wave 1 integration:

| File | Resolution |
| --- | --- |
| `.kiro/skills/fx-lookahead-bias-audit/SKILL.md` | Take dev's version (adds the `run_replay` gate and the `bias_checks.py` removal note) |
| `.kiro/skills/fx-replay-authenticity-gate/SKILL.md` | Take dev's version (says `run_replay` gates every result) |
| `fx_ai_engine/docs/research/validation-wave1-integration-notes.md` | Take dev's version ("Status: applied") |

Every other file merges cleanly. Re-run the full suite after the merge.

## Integration still to do in run_replay.py (dev owns that file)

- Wave 2 (`fx_ai_engine/docs/research/validation-wave2-integration-notes.md`):
  - (a) Give each batch its own output stem, so a later run doesn't overwrite earlier series. This is why 18 rows of `rsi_bounded`, `adx_20_25` and `trend_distance_5pip` are `SERIES_FILES_MISSING`.
  - (b) Record explicit `variant_name`, `exit_mode`, `halts` and `trades_csv` with its hash in each registry row.
  - (c) Optionally store the dev moments.
  - (e) Run R3 after each batch.
- Wave 3 (`validation-wave3-integration-notes.md`) and wave 4 (ADR 0005) need no `run_replay` change. They read the DB, bridge copies and replay CSVs.
- R3 depends on the registry label format `<variant>__<exit_mode>[__<halts>]` and the report and trades-file layout (wave 2 note (f)). Keep them, or update R3 together with them.
