---
name: fx-fill-reconciliation
description: Run the R7 fill reconciliation (research.fill_reconciliation) to compare each MetaTrader 5 demo deal with the fill the replay cost model would have assumed for the same signal (spread, slippage, latency, exit slippage, commission), measure systematic bias with a seeded bootstrap CI, and print a suggested cost-model adjustment as data only.
---

# FX fill reconciliation (R7)

## When to run

- Weekly during the demo burn-in (plan Phase 6), after copying the latest `deals.json` and M15 bar exports out of the terminal.
- Before trusting a replay result whose edge is thin, to check whether the replay's spread, slippage and commission assumptions are too kind to it.
- When checking the Phase 6 exit criterion that live costs stay within the replay's 1.5x cost assumption.

## Commands

Run from `fx_ai_engine/`. Build the synthetic fixture first (planted effects: spread +0.5 pip, entry slippage 0.3 pip against a 0.2 pip model, stop exits 0.4 pip adverse, commission equal to the model):

```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.synthetic_demo --out ../tmp/wave3-fixture --seed 3
USE_MT5_MOCK=1 ../.venv/bin/python -m research.fill_reconciliation --db ../tmp/wave3-fixture/trading_state.db --deals ../tmp/wave3-fixture/deals.json --bars-dir ../tmp/wave3-fixture/bars --feedback-dir ../tmp/wave3-fixture/feedback --out artifacts/research/wave3-fixture/r7
```

On real demo data, read only copies: a read-only snapshot of the state DB (`research.demo_records.snapshot_db`, which opens the live DB with `mode=ro`) and `cp` copies of `market/deals.json` and `market/bars_*_M15.json`:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.fill_reconciliation --db ../tmp/wave3-real/trading_state_snapshot.db --deals artifacts/research/wave3-real/inputs/deals.json --bars-dir artifacts/research/wave3-real/inputs --feedback-dir artifacts/demo-evidence/2026-11-03/run_160754 --out artifacts/research/wave3-real/r7
```

Options: `--cost-scenario` (`base`, `stress_1_5x`, `stress_2x`; default `base`), `--evidence-stream` (default `runtime_mt5_core_srs`), `--thresholds` (default `research/live_vs_replay_thresholds.json`). Outputs `fill_reconciliation.json` (report, inputs with sha256 prefixes, per-deal rows), `fill_reconciliation.md` and `deals.csv`.

## How to read the output

- Sign rule: every difference is in pips with BUY = +1 and SELL = -1, so a positive number always means the broker was worse for the trader than the model.
- `fill_diff_pips`: deal price minus the replay fill (bid open of the M15 bar that opens at the signal's bar close, plus modelled spread and slippage for a BUY). It combines spread and slippage error.
- `spread_diff_pips`: EA-reported spread at entry minus the modelled spread (bar spread with the per-symbol floor). `slippage_diff_pips`: slippage implied from the deal price and the reported spread, minus the model's 0.2 pip.
- `exit_slippage_diff_pips`: only for exits the EA tagged `[sl ...]` or `[tp ...]`. A stop is modelled with adverse slippage, a target at its level. `NOT_BY_EA` means the closing deal had another magic number and no tag (a likely manual close); it has no level to compare with.
- `fill_latency_s`: fill time minus bar close. The replay assumes zero, so any positive mean shows up as bias; read the size, not just the status.
- Status per metric: `INCONCLUSIVE` below `min_deals_for_bias` comparable deals (20 in thresholds v1), `SYSTEMATIC_BIAS` when the bootstrap CI of the mean excludes zero, otherwise `NO_SYSTEMATIC_BIAS`. The report verdict is `SYSTEMATIC_BIAS` if any metric has it.
- `cost_ratio`: live round-trip cost over model cost in pips, compared with `cost_ratio_limit`.
- `strict_reference`: the same deal against `replay_execution.market_entry` on the first bar opening after the decision. Informational; it does not enter the verdict.
- `suggested_adjustment`: mean implied slippage, mean commission per lot and a spread floor per symbol at the configured percentile of actual spreads, next to the current values. `actionable` is false whenever the verdict is `INCONCLUSIVE`.
- Not comparable rows: `NO_STRATEGY_SIGNAL` (deal with no trades row, such as EA demo-matrix test orders), `NO_DECISION_RECORD`, `NO_BAR_DATA`, and `NO_DEAL` (a closed or open trade with no entry deal, so missing fills are visible).

## FX/SRS specifics

- Report only. The tool never edits `backtesting/unified_replay.py` or `COST_SCENARIOS`; the suggestion is data for the owner. A real cost-model change is a separate, reviewed task.
- Pips come from `unified_replay.pip_size` (0.01 for JPY pairs, 0.0001 otherwise) and the USD pip value from `pip_value_usd_per_lot`, so USDJPY, USDCAD and USDCHF commissions convert with the deal price.
- `deals.json` `time_msc` is UTC. Exit files carry server time (UTC+2 on MetaQuotes-Demo), so exit times come from the closing deal.
- Thresholds were frozen in `live_vs_replay_thresholds.json` before any real data was read (ADR 0004). Change them only by adding a new version with a reason.
- Always `USE_MT5_MOCK=1`. Never `main.py --mode demo`, never an MT5 `trade_*` tool. The CLI refuses an `--out` inside any `MQL5/Files/bridge` folder.

## Pitfalls

- MetaQuotes-Demo spreads and fills are not a funded broker's. A suggestion measured on demo needs re-measuring on the funded account before it means anything.
- With the few real trades available the result is `INCONCLUSIVE`. Report it that way; do not quote a mean from two deals as a bias.
- The EA's reported spread is a single tick at fill time. A deal with no reported spread falls back to `trades.spread_exec_price`; check `actual_spread_source`.
- The bridge bar export holds only the last 400 M15 bars. Copy the bars soon after the trades, or old deals become `NO_BAR_DATA`.
- `price_mismatch_pips` set on a row means the deal price differs from the recorded entry price by more than 0.05 pip; reconcile the records before reading that row.

## Inspiration

Ideas, not text, from [Kryhr/26-quant-trading-skills](https://github.com/Kryhr/26-quant-trading-skills) (`backtest-vs-broker-fill-reconciliation`).
