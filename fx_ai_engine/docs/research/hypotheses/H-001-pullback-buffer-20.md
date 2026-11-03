# H-001: pullback_buffer_pips 20 vs 2

- ID: H-001
- Parameter: `AGENT_THRESHOLDS.TECHNICAL.pullback_buffer_pips`
- Active value: 20.0, from `fx_ai_engine/config_microcapital.py` `_SHARED_POLICY["AGENT_THRESHOLDS"]["TECHNICAL"]`
- Coded default: 2.0, from `fx_ai_engine/core/agents/technical_agent.py:25` (`TechnicalThresholds.pullback_buffer_pips`)
- Owner's original motivation: unknown, owner to fill (assessment §7 Q4)
- Status: pre-registered, not run
- Date written: 2026-11-03

## Mechanism

The BUY pullback test is `m15.low <= EMA(50) + buffer`; SELL mirrors it with `m15.high >= EMA(50) - buffer`. With a 20-pip buffer, nearly any M15 bar in an H1/H4-aligned trend satisfies it, so the filter no longer requires price to come back to the EMA. Entries then happen on extended bars as well as on pullbacks.

## Prediction

The 20-pip buffer increases trade count and lowers expectancy per trade relative to the 2-pip buffer, because extended entries have a worse stop-to-move ratio.

## Ablation command

From `fx_ai_engine/`:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m backtesting.run_replay --data-dir artifacts/datasets/<dataset> --variants baseline pullback_2 --costs base stress_1_5x
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant baseline
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant pullback_2
```

## Pre-registered rejection rule

Reject the 20-pip buffer if, on the development section, either:

- `baseline` `expectancy_r` is lower than `pullback_2` at both `base` and `stress_1_5x`; or
- `baseline` `expectancy_r` is ≤ 0 at `stress_1_5x`.

Inconclusive if either arm has fewer than 100 development trades or either arm's R2 gate verdict is not `PASS`.

## Result

(empty until both arms pass the R2 gate)
