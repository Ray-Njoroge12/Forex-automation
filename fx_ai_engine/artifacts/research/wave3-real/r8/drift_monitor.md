# Live vs replay drift (R8)

Report only: no halt or veto authority; INVESTIGATE asks a human to look.

Live demo R sequence placed inside bands simulated from the replay R distribution (research.monte_carlo.resample); thresholds from research/live_vs_replay_thresholds.json.

**Verdict: INCONCLUSIVE** (N_LIVE_BELOW_MIN (1 < 25))

Live closed trades: 1 (minimum for a verdict: 25). Replay trades: 6406. Mean R live +0.4926 vs replay -0.3476.
Simulation: 10000 paths, method block, block length 5, seed 11. Thresholds version 1 (sha256_16 04b13538c8afd78e).

Replay gate: replay distribution not gated by R2 (NOT EVIDENCE) (evidence status NOT_EVIDENCE).

Inputs: DB `../tmp/wave3-real/trading_state_snapshot.db` (stream runtime_mt5_core_srs, read-only); replay `../tmp/wave3-real/baseline__policy__edge__base_trades.csv` (sha256_16 2547c2ca3b247509, 6406 trades).

Flags below are informational only: the live sample is under the minimum.

| flag | rule | threshold | live rank | fired | note |
| --- | --- | --- | --- | --- | --- |
| CUM_R_BELOW_BAND | 100*P(sim <= live) < cum_r_low_percentile | 5 | 73.66 | no |  |
| CUM_R_ABOVE_BAND | 100*P(sim < live) > cum_r_high_percentile | 99 | 73.66 | no | check recording (live results better than almost every simulated path) |
| DRAWDOWN_ABOVE_BAND | 100*P(sim < live) > max_dd_r_high_percentile | 95 | 0.00 | no |  |
| LOSS_STREAK_ABOVE_BAND | 100*P(sim < live) > loss_streak_high_percentile | 95 | 0.00 | no |  |

| metric | live | p5 | p25 | p50 | p75 | p95 | 100*P(sim <= live) | 100*P(sim < live) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cum_r | +0.493 | -1.273 | -1.138 | -1.068 | +0.529 | +1.503 | 73.66 | 73.66 |
| max_dd_r | +0.000 | +0.000 | +0.000 | +1.068 | +1.138 | +1.273 | 36.96 | 0.00 |
| loss_streak | +0.000 | +0.000 | +0.000 | +1.000 | +1.000 | +1.000 | 36.96 | 0.00 |

Cumulative R bands per live trade (full table in bands.csv):

| k | p5 | p25 | p50 | p75 | p95 | live cum R | live rank |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | -1.27 | -1.14 | -1.07 | +0.53 | +1.50 | +0.49 | 73.66 |
