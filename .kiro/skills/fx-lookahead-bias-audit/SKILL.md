---
name: fx-lookahead-bias-audit
description: Run the R1 lookahead and recursive-bias audit (research.bias_audit) on FX bar data before trusting any replay result, after changing an agent, the replay feed or indicator code, or when someone asks whether the replay can see the future or whether 350 bars is enough warm-up for EMA(200).
---

# FX lookahead and recursive-bias audit (R1)

## When to run

- Before quoting any replay number, and again whenever `core/agents/**`, `core/indicators/**` or `backtesting/unified_replay.py` changes.
- When a new history export or bridge bar export becomes available; real-frame warm-up results need at least 2,000 bars per timeframe.
- As part of the R2 gate (`fx-replay-authenticity-gate` and `backtesting.run_replay` run it for you). Run it on its own when you need the detail.
- This is the only look-ahead and warm-up tool. The older `backtesting/bias_checks.py` was removed (ADR 0003); its 2026-11-03 output `artifacts/datasets/mt5_2026-11-03/bias_checks.json` is kept as historical evidence.

## Commands

Run from `fx_ai_engine/`:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.bias_audit --bridge-dir tests/fixtures/live_bars_2026-11-03 --max-decisions 60 --synthetic-long-bars 2400 --seed 7 --out artifacts/research/fixture-demo/r1
```

For a CSV history export use `--data-dir artifacts/datasets/<dataset>` instead of `--bridge-dir`. Other options: `--symbols`, `--variant` (a key of `run_replay.VARIANTS`, default `baseline`), `--start/--end` (ISO UTC), `--long-bars` (default 2000), `--eval-points` (default 200). `--max-decisions` is per symbol: half candidate times, the rest seeded non-candidate session times.

Exit code 0 means PASS; anything else (FAIL or INCONCLUSIVE) returns 1.

## How to read the output

`r1_report.json` fields:

- `verdict`: `PASS`, `FAIL` or `INCONCLUSIVE`.
- `truncation.status`: (a) the decision at bar close t is identical when bars not closed by t are removed. Any mismatch is listed in `by_symbol[].examples` with both fingerprints.
- `future_corruption.random` / `.nan`: (b) the same with those bars scaled randomly or set to NaN. An exception on altered data counts as a mismatch.
- `warmup_parity`: (c1) every agent fetch was served `min(requested, closed)` bars. `live_fetch_bars` holds the requested lengths seen (350 today). FAIL here means live and replay compute indicators on different windows.
- `warmup_deviation.frames[]`: (c2) per symbol and timeframe, `indicators.<name>.p95[L]` is the 95th-percentile gap (EMA in pips, ADX/RSI in points) between the value on the last L bars and on all history; `required_warmup` is the smallest L under threshold; `at_live_fetch` judges the live length. `synthetic[]` entries are `model_estimate` (seeded GBM) and never decide the verdict.
- `decision_flips`: decisions at the live length vs a lookback of all bars. Reported only.
- `sequential_vs_pointwise`: candidates from a sequential run (regime cache) equal pointwise evaluation. Reported only; a FAIL points to a cache bug.

INCONCLUSIVE with (a), (b), (c1) PASS means no real frame had 2,000 bars. It is not a pass.

## FX/SRS specifics

- Six majors only: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF. JPY pairs use a 0.01 pip; deviations are converted per symbol.
- Strategy inputs are completed bars only: a bar is visible once `open + timeframe <= t`. The partially formed H1/H4/D1 bar is treated as future data.
- Indicator periods come from the policy's `AGENT_THRESHOLDS`; locked constants are read from `config_microcapital.py`, never typed or changed.
- Always set `USE_MT5_MOCK=1`. Never run `main.py --mode demo`, never call an MT5 `trade_*` tool, never write to the terminal bridge folder.

## Pitfalls

- The fixture has 400 bars per timeframe, so real-frame warm-up is always INCONCLUSIVE there. Do not report the synthetic numbers as evidence.
- A leak that only shows on some bars needs enough decisions; raise `--max-decisions` on long data.
- If parity fails, check `PointInTimeFeed.lookback_override` is unset; it exists for bias checks only.
- The audit cannot fix a warm-up shortfall. The lever is the live fetch length in `core/agents`, which needs that path's owner (ADR 0002 V10).

## Inspiration

Ideas, not text, from [Kryhr/26-quant-trading-skills](https://github.com/Kryhr/26-quant-trading-skills) (`lookahead-bias-detection-methodology`) and the "Time Machine" replay idea in [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT).
