---
name: fx-multiple-testing
description: Run the R3 multiple-testing check (research.multiple_testing) over the experiment registry before naming any replay variant a winner; it deflates each variant's Sharpe for the number of configurations tried, estimates the probability of backtest overfitting and enforces a minimum trade count.
---

# FX multiple-testing check (R3)

## When to run

- Before quoting the best arm of an ablation (variant, exit mode, cost profile) anywhere: a report, an ADR, a hypothesis card's Result section.
- After every `backtesting/run_replay.py` batch, once its registry rows are written.
- Before freezing a configuration at gate G2.

## Commands

Run from `fx_ai_engine/`. On the registry this checkout writes to:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.multiple_testing --out artifacts/research/r3/<name>
```

On another checkout's registry (read-only; keep `--out` inside your own worktree):

```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.multiple_testing --registry /Users/raynj/Desktop/Forex-automation/fx_ai_engine/artifacts/experiments/registry.jsonl --out artifacts/research/real-2026-11-03/r3
```

Synthetic check of the tool itself:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.multiple_testing_fixture --out /tmp/r3-synthetic
USE_MT5_MOCK=1 ../.venv/bin/python -m research.multiple_testing --registry /tmp/r3-synthetic/artifacts/experiments/registry.jsonl --out artifacts/research/fixture-demo/r3
```

`--root` sets the base for the rows' `report` paths (default: three levels above the registry file, i.e. `fx_ai_engine/`). Exit code 0 means PASS; 1 means FAIL or INCONCLUSIVE. Outputs: `verdict.json` and `report.md` in `--out` (default `artifacts/research/r3/<UTC run id>`).

## How to read the output

`verdict.json`:

- `verdict`: `PASS`, `FAIL` or `INCONCLUSIVE`; `evidence_status`: `EVIDENCE_ELIGIBLE` only for PASS, otherwise `NOT_EVIDENCE`. Overall PASS means at least one row passed and the registry parsed cleanly.
- `trials_n` and `trial_keys`: N is the number of distinct `(policy_hash, exit_mode)` pairs in the registry, including superseded rows and rows without files.
- `thresholds`: `dsr_min` 0.95, `pbo_max` 0.20, `min_trades` and the `min_sample_design` it came from, `cscv_blocks` 16, `min_weeks` 64.
- `groups[]`, one per `(halts, cost)`: `series_labels`, `missing_labels`, `sr_variance` (V), `sr0`, `cscv` (`pbo`, `splits`, `n_rows` = weeks, `reason`).
- `variants[]`, one per evaluated `(label, cost)`: `dev_trades`, `expectancy_r`, `sharpe_per_trade`, `skew`, `kurtosis`, `psr0`, `sr0`, `dsr`, `pbo`, `verdict`, `evidence_status`, `reasons` (one code), `files` with sha256_16.
- `superseded_rows[]`, `missing_inputs[]`, `reasons[]` (registry codes plus a count per row reason), `notes[]`.

Row reason codes, checked in this order; the first that applies is the row's reason:

1. `SERIES_FILES_MISSING`, `SERIES_UNREADABLE`, `SERIES_RUN_MISMATCH` (the replay JSON is from another run, policy or cost), `SERIES_COUNT_MISMATCH` (dev/holdout counts differ from the registry, e.g. a file still being written): INCONCLUSIVE.
2. `BELOW_MIN_SAMPLE`: fewer dev trades than `min_trades`: INCONCLUSIVE.
3. `DSR_UNDEFINED`: zero variance or an invalid PSR denominator: INCONCLUSIVE.
4. `PSR_BELOW_MIN`: P(Sharpe > 0) < 0.95: FAIL.
5. `GROUP_TOO_FEW_SERIES`, `CSCV_TOO_SHORT`, `CSCV_GRID_MISMATCH`: the group cannot give V or PBO: INCONCLUSIVE.
6. `GROUP_SERIES_INCOMPLETE`: some configuration in the group has no valid series: INCONCLUSIVE.
7. `DSR_BELOW_MIN`: FAIL. 8. `PBO_ABOVE_MAX`: FAIL. 9. none: PASS.

Registry codes `REGISTRY_MISSING`, `REGISTRY_EMPTY` and `REGISTRY_LINE_UNPARSEABLE` make the overall verdict INCONCLUSIVE whatever the rows say.

`report.md` starts with `# Multiple-testing check (R3): PASS` or `# NOT EVIDENCE: no variant passed the multiple-testing check (R3)`, and its row table has an `evidence` column.

**Rule:** a variant is evidence only if its R3 row is PASS with the same `policy_hash` as the report you quote, and the same replay also has an R2 authenticity-gate PASS with matching hashes (skill `fx-replay-authenticity-gate`). Anything else is NOT EVIDENCE.

## FX/SRS specifics

- Development trades only (`entry_time < holdout_start`); the holdout is never read for the verdict, so it stays usable for one final check.
- Returns are `r_multiple` per trade, so compounding and position size do not change the statistics.
- CSCV uses FX weeks: each trade goes to the week that opens Sunday 17:00 New York, the same weekly boundary R2 uses for the closed market.
- `min_trades` is derived in code from the plan's target expectancy (D2, +0.25R) and `MIN_RISK_REWARD` read from `get_policy_config(mode_id="core_srs", env={})`: the dev count at which a 95% interval excludes zero with 80% power at the target (307 with today's constants). Locked constants are read from config, never typed or changed.
- Always `USE_MT5_MOCK=1`. The tool runs no replays; do not start one to fill a gap. Never `main.py --mode demo`, never the terminal bridge folder, never an MT5 `trade_*` tool.

## Pitfalls

- A batch that is still running shows up as `SERIES_COUNT_MISMATCH`, `SERIES_FILES_MISSING` or a partial registry line. Rerun after it finishes; do not edit the registry to make it parse.
- Batches written to a shared `--out` directory overwrite each other's files; older rows then lose their series. Their configurations still count toward N.
- Stress-cost rows are the same configuration at a higher cost, not extra trials. They are judged in their own cost group.
- SRS-halt runs stop trading after the first halt and form their own group; with one SRS series the group has no PBO, so those rows cannot pass.
- A low PBO among configurations that all lose says the ranking is stable, not that any of them works. Read it together with PSR and DSR.
- Do not lower `DSR_MIN`, `PBO_MAX` or the target expectancy to turn a verdict into PASS. Changing them is an owner decision recorded in an ADR.

## Inspiration

Ideas, not text, from [Kryhr/26-quant-trading-skills](https://github.com/Kryhr/26-quant-trading-skills) (`statistical-significance-and-deflated-metrics`). Methods: Bailey and López de Prado (2014), the deflated Sharpe ratio; Bailey, Borwein, López de Prado and Zhu (2017), the probability of backtest overfitting.
