---
name: fx-cost-stress
description: Run the R4 cost stress (research.cost_stress) to find the spread-plus-slippage multiple at which replay expectancy reaches zero, and to compare a fixed-trade-set re-cost with a full re-run at 1x, 1.5x, 2x and 3x costs.
---

# FX cost stress (R4)

## When to run

- Whenever a replay shows positive expectancy: check it survives 1.5× and 2× costs before anyone quotes it.
- When comparing variants: an edge that disappears at 1.5× is not an edge on a retail account.
- When the cost model, spread floors or broker change.

## Commands

Run from `fx_ai_engine/`:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.cost_stress --bridge-dir tests/fixtures/live_bars_2026-11-03 --multiples 1 1.5 2 3 --out artifacts/research/fixture-demo/r4
```

On a history export use `--data-dir artifacts/datasets/<dataset>`. Other options: `--variant`, `--exit-mode policy|fixed_tp|hybrid`, `--start/--end`, `--scale-commission`. Outputs: `cost_stress.json`, `cost_stress.md`, `trades_1x.csv` (the 1× replay trades, input for `fx-monte-carlo-risk-sweep`).

## How to read the output

- `recost`: the base trade set held fixed. `rows[].expectancy_r` per multiple; `breakeven_multiple` (number, or null) and `breakeven_label` (`"2.35x"` or `"<=1.0"`); `negative_at_base` is true when expectancy is already ≤ 0 at 1×. This isolates the cost effect.
- `rerun`: stage 2 re-simulated with costs scaled. `trades`, `expectancy_r`, `expectancy_r_ci95`, `profit_factor`, `net_usd` per multiple; `expectancy_r` is null with 0 trades. This includes the selection effect: the adversarial spread filter (`ADV_SPREAD_TOO_WIDE`) blocks entries as spreads widen, and sizing changes with balance.
- `notes`: what `run_replay` already provides and what this tool adds.

Re-cost model: `R(m) = r − (m − m0) × (spread_1x + 2 × slippage_1x) / stop_pips`, where `m0` is the cost multiple the trades were replayed at (`recost.replay_cost_multiplier`: 1.0 for `base`, 1.5 for `stress_1_5x`). Multiples are always absolute (1× = base cost), so a `stress_1_5x` replay's 2× row means 2× base cost, and its 1.5× row is its realised expectancy when commission is scaled. Rows with `below_replay_cost: true` add cost back out of the realised trades. Spread is taken from the entry bar for BUY (ask paid at entry) and from the exit bar for SELL. Exact under the linear model; approximate for trades with a partial close.

Commission: held at 1× by default (the task's spread-plus-slippage definition), whatever multiple `k` the replay applied (`recost.replay_commission_multiplier`); the 1× commission is `commission_usd / k`. `--scale-commission` sets commission to `m` instead, matching `run_replay`'s `CostModel`, which scales spread, slippage and commission together (profiles `base`, `stress_1_5x`, `stress_2x`). `recost_trades` must be given the cost model the trades were replayed with.

The R2 gate uses the re-cost at 2×: it passes only when expectancy there is above 0.

## FX/SRS specifics

- Six majors; spreads are `max(recorded, floor)` per symbol with floors in `unified_replay.DEFAULT_SPREAD_FLOOR_PIPS`; slippage 0.2 pip and $7/lot round trip at 1×.
- Completed bars only; the tool re-uses the same replay functions as `run_replay`.
- Locked constants are read from `config_microcapital.py`, never typed or changed.
- Always `USE_MT5_MOCK=1`. Never `main.py --mode demo`, never an MT5 `trade_*` tool, never the terminal bridge folder.

## Pitfalls

- A falling re-run trade count is not robustness. Read the re-cost row at the same multiple.
- The fixture (four trading days) has negative expectancy at 1×, so its break-even is `<=1.0`. Do not read anything into fixture numbers.
- Compare like with like: state whether commission was scaled.

## Inspiration

Ideas, not text, from [Kryhr/26-quant-trading-skills](https://github.com/Kryhr/26-quant-trading-skills) (`transaction-cost-and-market-impact-modeling`).
