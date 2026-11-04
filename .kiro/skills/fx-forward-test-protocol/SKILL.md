---
name: fx-forward-test-protocol
description: Run the R10 forward-test protocol for each demo test-wave release (ADR 0007). Freeze the release, seal pre-registered go/no-go and abort criteria before the wave starts, check the freeze daily, and evaluate the release's recorded demo trades read-only, getting GO, NO_GO, ABORT, INCONCLUSIVE, INVALID or REFUSED.
---
# FX forward-test protocol (R10)
**Owner direction (ADR 0007, 2026-11-04):** there is no 30-day demo. Each owner-started demo test wave runs one frozen release, and each release is evaluated on its own. Read "the demo" below as "the release under test". The 30-day clock and the 30-trading-day minimum do not apply. A minimum closed-trade count still does.
## When to run
- At the start of each owner-started test wave: freeze the release and seal that release's criteria.
- Every trading day of the wave: `freeze check`. Weekly and at the end of the wave: the evaluator.
- Any change to a parameter, the risk, the strategy or the EA ends the release. A new freeze and a new seal start the next one. Do not pool results across releases with different policy hashes.
## Commands
Run from `fx_ai_engine/` with the repo venv. Every command is read-only against the checkout and the database. Each writes only the file named by `--out` or `--report`.
```
# 1. Freeze, at the commit and EA build the demo will run (default EA build: the terminal's MQL5/Experts/FX_Execution.ex5)
../.venv/bin/python -m research.forward_test.freeze create --out artifacts/forward-test/<run>/freeze_manifest.json --label "<run>"
# 2. Owner approves the criteria file (status APPROVED, approved_by, freeze_manifest_sha256), then seal it while the start is in the future
../.venv/bin/python -m research.forward_test.prereg seal --criteria <criteria.json> --start 2026-12-01T00:00:00Z --out artifacts/forward-test/<run>/seal.json
# 3. Daily: is the running configuration still the frozen one?
../.venv/bin/python -m research.forward_test.freeze check --manifest artifacts/forward-test/<run>/freeze_manifest.json
# 4. Weekly and at the end: evaluate (the DB is opened with SQLite mode=ro; a copy also works)
USE_MT5_MOCK=1 ../.venv/bin/python -m research.forward_test.evaluate --criteria <criteria.json> --seal artifacts/forward-test/<run>/seal.json \
    --db database/trading_state.db --freeze-manifest artifacts/forward-test/<run>/freeze_manifest.json
```
For a fixture demo, build `/tmp/fx-r10-fixture.db` with `sqlite3 /tmp/fx-r10-fixture.db < tests/fixtures/forward_test/demo_trades.sql`. Then evaluate it with `--as-of 2027-02-15T00:00:00Z` against a seal whose start is `2027-01-04T00:00:00Z`. The starter file `docs/research/forward-test/criteria-srs-v1.1-draft.json` is a DRAFT and not approved. Use it for demos only.
## How to read the output
- Freeze check: MATCH, or MISMATCH with one line per reason code and item. The codes are `POLICY_HASH_CHANGED`, `CONFIG_HASH_CHANGED`, `EA_SOURCE_HASH_CHANGED`, `EA_PIN_POLICY_MISMATCH`, `EA_BUILD_HASH_CHANGED` / `EA_BUILD_MISSING`, `PROTECTED_FILE_CHANGED` / `_ADDED` / `_REMOVED`, `GIT_COMMIT_CHANGED` and `POLICY_HASH_UNAVAILABLE`. INVALID_MANIFEST means the manifest was edited or has another schema.
- Pre-registration: `CRITERIA_MODIFIED_AFTER_START` (content changed and mtime after the start) or `CRITERIA_HASH_MISMATCH` (content changed) make the evaluator REFUSE. `BACKDATED_SEAL` means the seal was made after the start, so the result is not pre-registered evidence.
- Evaluator verdicts, in priority order:
  - REFUSED: the criteria changed or the seal is bad.
  - INVALID: the freeze no longer matches.
  - ABORT: an abort criterion triggered. Each abort criterion can wait for its own `min_trades`.
  - INCONCLUSIVE: below the pre-registered trade or trading-day minimum, or a go criterion is open or cannot be computed.
  - NO_GO: a go criterion failed.
  - GO: every automated go criterion passed. The manual checks (orders only from the EA, no hard-risk failure, spread/slippage controls, live costs vs the replay's 1.5×) still need owner sign-off.
- `decision_grade: false` (the report opens with "NOT A DECISION") whenever the criteria are not APPROVED or the seal is backdated.
- Metrics come from existing code. `win_rate`, `avg_r_all`, `avg_r_winners_srs` and `max_drawdown` (equity, account_metrics) come from `validation.validate_demo`. `expectancy_r` with its bootstrap CI, `profit_factor` (account currency), `avg_winner_r` / `avg_loser_r` (±0.1R break-even band) and `max_consecutive_losses` come from `backtesting.replay_report.trade_metrics`.
## FX/SRS specifics
- The tools never change a constant, the SRS or the EA. The SRS v1 lines in the criteria file (45% / 40% win rate, 15% / 20% drawdown, 25 trades) must equal `validation.validate_demo`'s constants. The D2 values (+0.25R, PF 1.3, average winner 1.5R) are the plan's proposed defaults, not approved. D2, D3 and O1–O5 in `docs/2026-11-04-srs-v1.1-draft-proposal.md` are owner decisions.
- The policy hash is computed by running `validation.print_policy_hash --mode core_srs` in the target checkout with this shell's environment. Run `freeze check` with the engine's environment, or env overrides will show as `POLICY_HASH_CHANGED`.
- Scope defaults to `policy_mode=core_srs`, `execution_mode=mt5`, as in `validate_demo`'s core profile. Only `CLOSED*` trades inside [start, as-of] count. Unresolved `execution_feedback_conflicts` count whenever they were created.
- Always `USE_MT5_MOCK=1` for the evaluator. Never `main.py --mode demo`, never an MT5 `trade_*` tool, never write to the terminal bridge folder, never start or stop the engine or watchdog.
## Pitfalls
- A seal proves nothing if it can be rewritten. Commit the criteria file and the seal before the start, so the commit date anchors them.
- Protected-path hashes cover code files only (.py, .mq5, .mqh, .md). Strategy code outside the protected paths (for example `core/agents`) is covered only by `GIT_COMMIT_CHANGED`, so uncommitted edits there are not seen.
- The compiled `.ex5` hash is the file in `MQL5/Experts`. MT5 keeps running the old copy until the EA is re-attached, so check the EA journal as well.
- Reading a WAL database touches its `-shm` file, as every SQLite reader does. Evaluate a copy if that matters.
- A point-estimate gate at the target value (for example expectancy ≥ +0.25R) passes a strategy whose true expectancy is exactly that value only about half the time. Small samples are noisy: the fixture's 30 trades at +0.30R have a 95% CI of −0.20R to +0.80R.
## Inspiration
Ideas, not text, from [Kryhr/26-quant-trading-skills](https://github.com/Kryhr/26-quant-trading-skills) (`paper-trading-and-forward-test-protocol`, `capital-allocation-and-strategy-lifecycle`).
