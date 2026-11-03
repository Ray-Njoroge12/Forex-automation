---
name: fx-monte-carlo-risk-sweep
description: Run the R5 Monte Carlo risk sweep (research.monte_carlo) on replay trades to estimate the probability of each SRS halt and of breaching the acceptance drawdown at risk-per-trade levels from 1.0% to 3.2%, with i.i.d. and block bootstrap; evidence for plan decision D3.
---

# FX Monte Carlo risk sweep (R5)

## When to run

- When deciding plan decision D3 (keep 3.2% risk per trade, or lower to 1.0–1.5% through SRS v1.1; ADR 0001).
- After any replay whose trades pass the R2 gate, to see how its R distribution behaves under the SRS halts over a 30-trade demo and a 100-trade horizon.

## Commands

Run from `fx_ai_engine/`, on a trades CSV with `r_multiple` and `exit_time` (from `fx-cost-stress` or `run_replay`'s `*_trades.csv`):

```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.monte_carlo --trades-csv artifacts/research/fixture-demo/r4/trades_1x.csv --runs 10000 --seed 11 --out artifacts/research/fixture-demo/r5
```

Options: `--block-length` (default 5), `--risks` (default 1.0, 1.5, 2.0, 2.5, 3.0% plus `BASE_RISK_PCT` from the policy), `--trade-counts` (default 30 100), `--trades-per-day` (default: rounded mean per active UTC day in the CSV). Outputs `monte_carlo.json` and `monte_carlo.md`. Same seed, same output.

## How to read the output

- `limits`: halts read from the policy (`daily_stop_pct`, `weekly_stop_pct`, `hard_drawdown_pct`, `loss_halt_threshold`, `loss_throttle_steps`, `base_risk_pct`) and `acceptance_max_drawdown` from `validation.validate_demo.MAX_DRAWDOWN` (SRS 12.2).
- `rows[]` per `method` (`iid` or `block`), `risk` and `trades`: `p_daily_stop`, `p_weekly_stop`, `p_drawdown_halt`, `p_loss_streak_halt` (probability the halt fires at least once), `p_max_dd_gt_acceptance`, and p5/p25/p50/p75/p95 bands of `max_drawdown` and `final_equity` (multiple of start).
- `legacy_iid_reference`: the existing `replay_report.monte_carlo_drawdown` at `BASE_RISK_PCT`, for comparison. It has no halts, so its drawdowns run past the 20% stop.

Block vs i.i.d.: the block bootstrap resamples runs of consecutive trades (sorted by exit time), so loss streaks and regime clustering survive. If block probabilities are much higher than i.i.d., losses cluster and i.i.d. understates risk.

Path rules: risk × throttle by current loss streak; after each trade, daily stop skips the day, weekly stop skips to next week, the drawdown halt ends the path, a 3-loss streak skips to next week and resets (the replay's operator reset). Skipped slots are not traded.

## FX/SRS specifics

- This tool never changes a risk constant. Every limit is read from `config_microcapital.py`; never type them in or edit them. A D3 change goes through SRS v1.1 and the owner.
- The input must come from a replay of the six majors on completed bars; quote it only if that replay passed the R2 gate.
- Always `USE_MT5_MOCK=1`. Never `main.py --mode demo`, never an MT5 `trade_*` tool, never the terminal bridge folder.

## Pitfalls

- Resampling cannot create an edge the sample lacks, and a small sample (the fixture has 19 trades) gives unstable bands.
- `trades_per_day` changes daily-stop probabilities a lot; state the value used.
- The block bootstrap needs the CSV in time order; the CLI sorts by `exit_time`.

## Inspiration

Ideas, not text, from [Kryhr/26-quant-trading-skills](https://github.com/Kryhr/26-quant-trading-skills) (`monte-carlo-trade-resampling-and-drawdown-distribution`).
