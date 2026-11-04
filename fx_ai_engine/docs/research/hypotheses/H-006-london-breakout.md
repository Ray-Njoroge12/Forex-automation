# H-006: London-open breakout of the Asian range vs trend_pullback_v1
- ID: H-006
- Strategy: `london_breakout_v1`, `fx_ai_engine/core/strategy/london_breakout.py`
- Frozen parameters: range 00:00–07:00 Europe/London (28 M15 bars required; on the two UK DST-change days the count follows the elapsed time, 24 or 32); entry decisions (07:00, 10:00] London; first close outside the range only; stop at opposite range side ± 1 pip, accepted 20–60 pips; fixed 2.2R spread-adjusted target; no break-even, partial or trail; reject if the H1 regime trends against the breakout. Code defaults (`Params`): `range_start_hour=0`, `range_end_hour=7`, `entry_end_hour=10`, `min_stop_pips=20`, `max_stop_pips=60`, `stop_buffer_pips=1`
- Comparators: `trend_pullback_v1` baseline (active core_srs policy, policy and fixed_tp exits); RANDOM and INVERTED direction controls at H-006 entry times (`edge_diagnostics --strategy london_breakout_v1`)
- SRS fit: within SRS v1 (H1 regime, M15 execution, M15 structure stop, R:R ≥ 2.2); registered `live_eligible=True`, but live runs `trend_pullback_v1` only (ADR 0014)
- Proposed by / approved: stage D design 2026-11-04 / owner, 2026-11-04
- Family size k: 3 (H-006, H-007, H-009), Bonferroni z = 2.39
- Status: pre-registered, not run
- Date written: 2026-11-04
## Mechanism
The Asian session range marks where overnight liquidity cleared. European order flow at the London open either holds price inside it or pushes through. A close outside the range on the first attempt is the event; the stop sits on the far side of the range, a structural level 20–60 pips away, so the round-trip cost is about 0.04–0.12R instead of the baseline's 0.31R (Phase 2 E3, E4). A fixed 2.2R bracket keeps the whole winner (E5: the live exits cap the average winner at 1.0R).
## Prediction
At zero cost the breakout direction beats its RANDOM-direction control at the same entry times; after 1.5× costs and the swap allowance, development expectancy is ≥ +0.25R with profit factor ≥ 1.3 and average winner ≥ 1.5R.
## Evaluation commands
From `fx_ai_engine/`, each via `python -m validation.detached_job start --name h006 -- …` (2 workers):
```
python -m backtesting.run_replay --data-dir artifacts/datasets/mt5_2026-11-03 --strategy london_breakout_v1 --variants baseline --exit-modes policy --costs base stress_1_5x stress_2x --halts edge
python -m backtesting.edge_diagnostics --data-dir artifacts/datasets/mt5_2026-11-03 --strategy london_breakout_v1
python -m backtesting.run_replay --data-dir artifacts/datasets/mt5_2026-11-03 --strategy london_breakout_v1 --variants baseline --exit-modes policy --costs base --halts srs
python -m research.swap_allowance --trades <arm stress_1_5x _trades.csv> --symbols-json <snapshot> --holdout-start <report holdout_start>
```
Baseline, once for the family: `python -m backtesting.run_replay --data-dir artifacts/datasets/mt5_2026-11-03 --variants baseline --exit-modes policy fixed_tp --costs base stress_1_5x --halts edge`. The run uses the defaults above and no `--strategy-params`; any parameter change means a new card. The `--halts srs` run is information only.
## Pre-registered rejection rule
Development section (2022-12-24 to the report's `holdout_start`, last 6 months held out), `stress_1_5x`, swap-adjusted. Inconclusive if < 100 development trades or the arm's R2 verdict is not PASS. For the baseline and control rows only the authenticity checks (R1A, R1B, R1C parity and the three fill checks) must pass. Not rejected only if all hold: expectancy ≥ +0.25R; PF ≥ 1.3; avg winner ≥ 1.5R; mean − 2.39·s/√n > 0 (family size k = 3); zero-cost expectancy − RANDOM zero-cost expectancy > 2·√(s₁²/n₁ + s₂²/n₂) and zero-cost expectancy > INVERTED's; expectancy > 0 in ≥ 60% of half-year windows with ≥ 20 trades; expectancy > 0 after removing the best symbol. Rejected if expectancy ≤ 0 at `stress_1_5x`, or if any rule above fails with ≥ 100 trades. Holdout read once afterwards: confirms if expectancy at `stress_1_5x` (swap-adjusted) > 0 with ≥ 30 trades; otherwise `inconclusive` or `contradicts`. Nothing is re-tuned afterwards. Also report the SRS §12.2 metrics, by-symbol and by-hour tables, Adversarial and PM rejection counts, median holding time and the number of registry rows in the family. A "not rejected" card with a confirming holdout does not change the live strategy by itself.
Known limitation: the replay has no news blackout; live would skip some London-open trades.
## Result
(empty until the arm passes the R2 gate)
