# H-002: RSI bounds 0–100 vs 40–65 buy / 35–60 sell

- ID: H-002
- Parameter: `AGENT_THRESHOLDS.TECHNICAL.buy_rsi_min`, `buy_rsi_max`, `sell_rsi_min`, `sell_rsi_max`
- Active value: 0.0 / 100.0 for both directions, from `fx_ai_engine/config_microcapital.py` `_SHARED_POLICY["AGENT_THRESHOLDS"]["TECHNICAL"]`
- Coded default: buy 40–65, sell 35–60, from `fx_ai_engine/core/agents/technical_agent.py:26-29` (`TechnicalThresholds`)
- Owner's original motivation: unknown, owner to fill (assessment §7 Q4)
- Status: pre-registered, not run
- Date written: 2026-11-03

## Mechanism

`rsi_ok = rsi_floor <= RSI(14) <= rsi_ceiling` on the last closed M15 bar. With bounds 0 and 100 the condition is always true, so the RSI filter is off. The coded bounds reject buys when M15 is overbought (RSI > 65) and sells when it is oversold (RSI < 35), which are late entries into a move.

## Prediction

Opening the bounds adds trades taken at stretched RSI, and those trades have lower expectancy than the trades the bounded filter keeps.

## Ablation command

From `fx_ai_engine/`:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m backtesting.run_replay --data-dir artifacts/datasets/<dataset> --variants baseline rsi_bounded --costs base stress_1_5x
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant baseline
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant rsi_bounded
```

## Pre-registered rejection rule

Reject the open RSI bounds if, on the development section, either:

- `baseline` `expectancy_r` is lower than `rsi_bounded` at both `base` and `stress_1_5x`; or
- `baseline` `expectancy_r` is ≤ 0 at `stress_1_5x`.

Inconclusive if either arm has fewer than 100 development trades or either arm's R2 gate verdict is not `PASS`.

## Result

(empty until both arms pass the R2 gate)
