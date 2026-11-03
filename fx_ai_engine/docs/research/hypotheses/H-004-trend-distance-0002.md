# H-004: trend_distance_threshold 0.0002 vs 0.0005

- ID: H-004
- Parameter: `AGENT_THRESHOLDS.REGIME.trend_distance_threshold`
- Active value: 0.0002, from `fx_ai_engine/config_microcapital.py` `_SHARED_POLICY["AGENT_THRESHOLDS"]["REGIME"]`
- Coded default: 0.0005, from `fx_ai_engine/core/agents/regime_agent.py:22` (`RegimeThresholds`)
- Owner's original motivation: unknown, owner to fill (assessment §7 Q4)
- Status: pre-registered, not run
- Date written: 2026-11-03

## Mechanism

The regime agent requires `abs(EMA50 - EMA200) >= trend_distance_threshold` on H1 before it labels a trend. The comparison is in price units, not pips. On non-JPY pairs 0.0002 is 2 pips (default 5 pips). On USDJPY, where one pip is 0.01, 0.0002 is 0.02 pip, so the gate is effectively off for JPY and nearly any EMA separation counts as a trend.

## Prediction

The lower threshold admits trends whose EMAs are barely separated, with lower expectancy than the 5-pip arm. The effect is largest on USDJPY, where the gate is practically disabled.

## Ablation command

From `fx_ai_engine/`:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m backtesting.run_replay --data-dir artifacts/datasets/<dataset> --variants baseline trend_distance_5pip --costs base stress_1_5x
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant baseline
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant trend_distance_5pip
```

Report the USDJPY row of the by-symbol breakdown (`development.by_symbol.USDJPY`) for both arms alongside the totals. Note that `trend_distance_5pip` (0.0005) is still 0.05 pip on USDJPY, so this ablation does not test a JPY-correct threshold; a per-pip threshold would need its own card.

## Pre-registered rejection rule

Reject 0.0002 if, on the development section, either:

- `baseline` `expectancy_r` is lower than `trend_distance_5pip` at both `base` and `stress_1_5x`; or
- `baseline` `expectancy_r` is ≤ 0 at `stress_1_5x`.

Inconclusive if either arm has fewer than 100 development trades or either arm's R2 gate verdict is not `PASS`.

## Result

(empty until both arms pass the R2 gate)
