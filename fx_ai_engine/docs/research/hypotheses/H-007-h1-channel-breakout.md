# H-007: Two-day H1 channel breakout in the trend regime vs trend_pullback_v1
- ID: H-007
- Strategy: `h1_channel_breakout_v1`, `fx_ai_engine/core/strategy/h1_channel_breakout.py`
- Frozen parameters: regime TRENDING_BULL → BUY only, TRENDING_BEAR → SELL only (active RegimeAgent thresholds); channel = high/low of the last 48 closed H1 bars; trigger = first M15 close through the channel in the trend direction; stop = 16-bar M15 swing ∓ 1 pip, accepted 20–60 pips; fixed 2.2R spread-adjusted target; no break-even, partial or trail. Code defaults (`Params`): `channel_h1_bars=48`, `stop_lookback_m15=16`, `min_stop_pips=20`, `max_stop_pips=60`, `stop_buffer_pips=1`. Because the channel uses closed H1 bars only, an M15 bar closing on the hour lies inside the last channel bar, so triggers occur at :15, :30 and :45
- Comparators: `trend_pullback_v1` baseline (same regime gate); RANDOM and INVERTED controls at H-007 entry times
- SRS fit: within SRS v1; registered `live_eligible=True`, but live runs `trend_pullback_v1` only (ADR 0004)
- Proposed by / approved: stage D design 2026-11-04 / owner, 2026-11-04
- Family size k: 3 (H-006, H-007, H-009), Bonferroni z = 2.39
- Status: pre-registered, not run
- Date written: 2026-11-04
## Mechanism
Same H1 trend label as the baseline, different entry event: a close through a two-day extreme in the trend direction instead of any bar near EMA50 (Phase 2 E7: the baseline fires on 33% of trending session bars). Only 9.6% of baseline entries reached 2.2R (E5); a breakout of a multi-day extreme should follow through more often if the trend label has value. The 16-bar swing stop puts costs at about 0.04–0.12R (E4).
## Prediction
Zero-cost expectancy beats RANDOM at the same entry times; after 1.5× costs and the swap allowance, development expectancy ≥ +0.25R, PF ≥ 1.3, average winner ≥ 1.5R. If it fails, the H1 trend label itself carries no usable direction at these horizons.
## Evaluation commands
As H-006 with `--strategy h1_channel_breakout_v1` and job name `h007`.
## Pre-registered rejection rule
As H-006 (same thresholds, same family correction, same holdout rule).
## Result
(empty until the arm passes the R2 gate)
