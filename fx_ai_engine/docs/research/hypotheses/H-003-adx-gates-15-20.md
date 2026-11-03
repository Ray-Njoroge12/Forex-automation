# H-003: ADX gates 15/20 vs 20/25

- ID: H-003
- Parameter: `AGENT_THRESHOLDS.REGIME.adx_no_trade_below`, `adx_transition_below`
- Active value: 15.0 / 20.0, from `fx_ai_engine/config_microcapital.py` `_SHARED_POLICY["AGENT_THRESHOLDS"]["REGIME"]`
- Coded default: 20.0 / 25.0, from `fx_ai_engine/core/agents/regime_agent.py:23-24` (`RegimeThresholds`)
- Owner's original motivation: unknown, owner to fill (assessment §7 Q4)
- Status: pre-registered, not run
- Date written: 2026-11-03

## Mechanism

The regime agent computes ADX(14) on H1. Below `adx_no_trade_below` the trend state becomes FLAT (no trade). Between that and `adx_transition_below`, a trend is downgraded to TRANSITION (no trade). Lower gates let weaker directional movement count as a tradeable trend, so more H1 bars qualify as TRENDING_BULL or TRENDING_BEAR.

## Prediction

The extra regimes admitted by 15/20 are weak trends whose follow-through is lower, so expectancy per trade falls relative to 20/25.

## Ablation command

From `fx_ai_engine/`:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m backtesting.run_replay --data-dir artifacts/datasets/<dataset> --variants baseline adx_20_25 --costs base stress_1_5x
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant baseline
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant adx_20_25
```

## Pre-registered rejection rule

Reject the 15/20 gates if, on the development section, either:

- `baseline` `expectancy_r` is lower than `adx_20_25` at both `base` and `stress_1_5x`; or
- `baseline` `expectancy_r` is ≤ 0 at `stress_1_5x`.

Inconclusive if either arm has fewer than 100 development trades or either arm's R2 gate verdict is not `PASS`.

## Result

(empty until both arms pass the R2 gate)
