# H-009: 20-day breakout trend following vs trend_pullback_v1
- ID: H-009
- Strategy: `d1_breakout_v1`, `fx_ai_engine/core/strategy/d1_breakout.py`
- Frozen parameters: BUY when the last closed D1 bar closes above the highest high of the previous 20 D1 bars (SELL mirrored); entry at the first session decision after that D1 close; reject if the H1 regime trends against; stop = 2.0 × ATR(20, D1), accepted 40–300 pips; fixed 2.2R spread-adjusted target; no break-even, partial or trail. Code defaults (`Params`): `channel_d1_bars=20`, `atr_d1_period=20`, `stop_atr_mult=2.0`, `min_stop_pips=40`, `max_stop_pips=300`
- Comparators: `trend_pullback_v1` baseline; RANDOM and INVERTED controls at H-009 entry times
- SRS fit: replay/research only. D1 direction and D1 ATR stop change the timeframe model (SRS §5.2, §15) and the stop basis (§5.3). The owner accepted in principle (2026-11-04) that an SRS v1.x revision may follow; until it is approved the strategy is registered `live_eligible=False` and `Engine` refuses to start with it (`tests/test_strategy_live_eligibility.py`). A "not rejected" result does not change this
- Proposed by / approved: stage D design 2026-11-04 / owner, 2026-11-04
- Family size k: 3 (H-006, H-007, H-009), Bonferroni z = 2.39
- Status: pre-registered, not run
- Date written: 2026-11-04
## Mechanism
Time-series momentum: currencies that make a new 20-day extreme tend to continue (Moskowitz, Ooi and Pedersen, JFE 2012; weaker in G10 FX in recent decades). With 100–200-pip stops the round-trip cost is about 0.01–0.03R (Phase 2 E3, E4), so even a small directional edge survives costs. Holding periods of days to weeks make swap material; the swap allowance applies.
## Prediction
Zero-cost expectancy beats RANDOM; after 1.5× costs and the swap allowance, development expectancy ≥ +0.25R, PF ≥ 1.3, average winner ≥ 1.5R. Main risk: fewer than 100 development trades (inconclusive).
## Evaluation commands
As H-006 with `--strategy d1_breakout_v1` and job name `h009`. Also report median holding days and the share of time both open-trade slots are occupied. Its replay reports carry `"live_eligible": false` and the line `Research only: …`.
## Pre-registered rejection rule
As H-006.
## Result
(empty until the arm passes the R2 gate)
