# Validation wave 2: integration notes for run_replay.py

Branch `research/validation-wave2-multiple-testing` adds `research/multiple_testing.py` (R3) without editing `backtesting/run_replay.py`; another session owns that file. These are the changes the run_replay owner would make so R3 can judge every batch. Nothing below has been applied. Decisions are in `docs/decisions/0006-validation-wave2-multiple-testing.md`.

(a) **Keep each batch's series.** `run_replay` defaults `--out` to `artifacts/replay/<run_id>`, which is safe. The 2026-11-03 batches were run with a shared `--out artifacts/replay/2026-11-03_edge`, so run `20261103T184434Z` overwrote the JSON and trades files of 18 rows from run `20261103T175026Z`. Those rows have a newer row for the same label and cost, so R3 lists them as superseded; if one were evaluated, its file would fail `SERIES_RUN_MISMATCH` (`generated_at` ≠ `run_id`), because R3 never uses a file that belongs to another run. The 18 rows of `rsi_bounded`, `adx_20_25` and `trend_distance_5pip` from that run have no files in the directory at all and are `SERIES_FILES_MISSING`. Either keep the per-run default, or put the run id in the stem:

```python
stem = out_root / f"{label}__{cost_name}__{run_id}"
```

(b) **Name the parts instead of encoding them in the label.** R3 parses `<variant>__<exit_mode>[__<halts>]`. Add explicit fields and the trades file binding to the registry row:

```python
trades_csv = stem.with_name(stem.name + "_trades.csv")
row.update({"variant_name": variant, "exit_mode": exit_mode, "halts": args.halts,  # halts on hardening/2026-11
            "trades_csv": str(trades_csv.relative_to(PROJECT_ROOT)), "trades_csv_sha256_16": _sha(trades_csv)})
```

R3 would then prefer these fields over label parsing and could check the hash instead of the counts (a follow-up change in `research/multiple_testing.py`, not made here).

(c) **Optional: store the dev moments.** With the per-trade dev `r_std`, `skew` (biased) and `kurtosis` (Pearson, biased) in the row, the cross-trial variance of Sharpe stays computable for runs whose files are gone. CSCV still needs the weekly series, so this only helps the DSR:

```python
from scipy import stats
r = np.array([t["r_multiple"] for t in dev], dtype=float)
row["dev"].update(r_std=float(r.std(ddof=1)), skew=float(stats.skew(r)), kurtosis=float(stats.kurtosis(r, fisher=False)))
```

(d) **`superseded`.** Neither this branch's nor hardening's run_replay writes `superseded`; the flags in the 2026-11-03 registry were written outside run_replay. R3 honours it if present (per `(label, cost)` it evaluates the last row without the flag, else the last row) and lists the others. If rows are to be retired, write the flag with a reason rather than deleting rows: deleted rows would lower N.

(e) **Run R3 after every batch.** At the end of `run_replay.main`, after `summary.json`:

```python
from research.multiple_testing import evaluate, render_markdown
r3 = evaluate(REGISTRY, PROJECT_ROOT)
write_json(out_root / "r3_verdict.json", r3)  # research.common.write_json
(out_root / "r3_report.md").write_text(render_markdown(r3), encoding="utf-8")
for row in summary:
    row["r3_verdict"] = next((v["verdict"] for v in r3["variants"]
                              if v["label"] == row["variant"] and v["cost"] == row["cost"]), None)
```

Or from the shell: `USE_MT5_MOCK=1 ../.venv/bin/python -m research.multiple_testing --out <batch dir>/r3`.

(f) **Coupling.** R3 depends on the label format `<variant>__<exit_mode>[__<halts>]` (exit modes from `unified_replay.EXIT_MODES`), the report path `report` relative to `fx_ai_engine/` with `<stem>.json` and `<stem>_trades.csv` beside it, the JSON keys `generated_at`, `policy_hash`, `cost`, `holdout_start`, `development.range` (`"YYYY-MM-DD to YYYY-MM-DD"`), the CSV columns `entry_time` and `r_multiple`, and the dev split `entry_time < holdout_start`. Changing any of these turns rows into `SERIES_*` INCONCLUSIVE until R3 is updated.

Results on this branch are in `artifacts/research/fixture-demo/r3/` (synthetic, PASS) and `artifacts/research/real-2026-11-03/r3/` (real registry, INCONCLUSIVE, NOT_EVIDENCE) and summarised in ADR 0006 M9.
