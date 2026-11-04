# Live vs replay drift (R8)

Report only: no halt or veto authority; INVESTIGATE asks a human to look.

Live demo R sequence placed inside bands simulated from the replay R distribution (research.monte_carlo.resample); thresholds from research/live_vs_replay_thresholds.json.

**Verdict: WITHIN_BANDS** (no flag fired)

Live closed trades: 36 (minimum for a verdict: 25). Replay trades: 200. Mean R live +0.1083 vs replay +0.0560.
Simulation: 10000 paths, method block, block length 5, seed 11. Thresholds version 1 (sha256_16 04b13538c8afd78e).

Replay gate: replay distribution not gated by R2 (NOT EVIDENCE) (evidence status NOT_EVIDENCE).

Inputs: DB `../tmp/wave3-fixture/trading_state.db` (stream runtime_mt5_core_srs, read-only); replay `../tmp/wave3-fixture/replay_trades.csv` (sha256_16 b60f6464c9afad17, 200 trades).

| flag | rule | threshold | live rank | fired | note |
| --- | --- | --- | --- | --- | --- |
| CUM_R_BELOW_BAND | 100*P(sim <= live) < cum_r_low_percentile | 5 | 59.29 | no |  |
| CUM_R_ABOVE_BAND | 100*P(sim < live) > cum_r_high_percentile | 99 | 59.29 | no | check recording (live results better than almost every simulated path) |
| DRAWDOWN_ABOVE_BAND | 100*P(sim < live) > max_dd_r_high_percentile | 95 | 12.48 | no |  |
| LOSS_STREAK_ABOVE_BAND | 100*P(sim < live) > loss_streak_high_percentile | 95 | 15.72 | no |  |

| metric | live | p5 | p25 | p50 | p75 | p95 | 100*P(sim <= live) | 100*P(sim < live) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cum_r | +3.900 | -10.400 | -4.000 | +2.400 | +8.800 | +15.200 | 59.29 | 59.29 |
| max_dd_r | +5.000 | +4.000 | +5.800 | +7.800 | +10.600 | +16.000 | 19.50 | 12.48 |
| loss_streak | +5.000 | +4.000 | +5.000 | +6.000 | +8.000 | +12.000 | 33.86 | 15.72 |

Cumulative R bands per live trade (full table in bands.csv):

| k | p5 | p25 | p50 | p75 | p95 | live cum R | live rank |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | -1.00 | -1.00 | -1.00 | +2.20 | +2.20 | +2.20 | 100.00 |
| 2 | -2.00 | -2.00 | +1.20 | +1.20 | +4.40 | +1.20 | 91.03 |
| 3 | -3.00 | -3.00 | +0.20 | +0.20 | +3.40 | +3.40 | 97.58 |
| 4 | -4.00 | -0.80 | -0.80 | +2.40 | +5.60 | +2.40 | 91.12 |
| 5 | -5.00 | -1.80 | +1.40 | +1.40 | +4.60 | +1.40 | 80.46 |
| 6 | -6.00 | -2.80 | +0.40 | +3.60 | +6.80 | +0.40 | 69.04 |
| 7 | -7.00 | -3.80 | -0.60 | +2.60 | +5.80 | -0.60 | 58.21 |
| 8 | -4.80 | -1.60 | +1.60 | +1.60 | +8.00 | -1.60 | 47.02 |
| 9 | -5.80 | -2.60 | +0.60 | +3.80 | +7.00 | +0.60 | 66.57 |
| 10 | -6.80 | -3.60 | -0.40 | +2.80 | +9.20 | +2.80 | 80.69 |
| ... | ... | ... | ... | ... | ... | ... | ... |
| 27 | -11.00 | -4.60 | +1.80 | +5.00 | +14.60 | +5.00 | 75.10 |
| 28 | -12.00 | -2.40 | +0.80 | +7.20 | +13.60 | +4.00 | 70.23 |
| 29 | -9.80 | -3.40 | +3.00 | +6.20 | +15.80 | +3.00 | 65.35 |
| 30 | -10.80 | -4.40 | +2.00 | +8.40 | +14.80 | +3.50 | 60.36 |
| 31 | -11.80 | -2.20 | +1.00 | +7.40 | +13.80 | +2.50 | 55.00 |
| 32 | -12.80 | -3.20 | +0.00 | +6.40 | +16.00 | +4.70 | 64.89 |
| 33 | -10.60 | -4.20 | +2.20 | +8.60 | +15.00 | +3.70 | 60.07 |
| 34 | -11.60 | -5.20 | +1.20 | +7.60 | +17.20 | +2.70 | 55.07 |
| 35 | -12.60 | -3.00 | +3.40 | +6.60 | +16.20 | +1.70 | 49.87 |
| 36 | -10.40 | -4.00 | +2.40 | +8.80 | +15.20 | +3.90 | 59.29 |
