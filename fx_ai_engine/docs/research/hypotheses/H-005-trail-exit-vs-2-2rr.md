# H-005: TRAIL exit mode vs the nominal 2.2 R:R

- ID: H-005
- Parameter: trade-management exit mode in trending regimes (`tp_mode`)
- Active value: `TRAIL`, returned by `TechnicalAgent._get_trade_management_params` (`fx_ai_engine/core/agents/technical_agent.py:437`) for TRENDING regimes, because `fx_ai_engine/config_microcapital.py` `_LIVE_TRADE_MGMT_OPTION_C["enabled"]` is False in the active policy
- Coded default: nominal fixed target at `min_rr = 2.2`, from `fx_ai_engine/core/agents/technical_agent.py:24` (`TechnicalThresholds.min_rr`) and SRS v1 minimum R:R
- Owner's original motivation: unknown, owner to fill (assessment §7 Q4)
- Status: pre-registered, not run
- Date written: 2026-11-03

## Mechanism

In trending regimes `_get_trade_management_params` returns `tp_mode=TRAIL` with break-even at 0.8R (+1 pip), a 50% partial close at 1.2R and an ATR trail of 1.5× ATR (HIGH volatility: 1.2R / 1.5R / 2.0×). `simulate` sets no hard target for TRAIL, so the 2.2R take-profit in the signal is never used. Realised R then clusters near zero (break-even exits) and at −1R (stops), with occasional trail runners.

## Prediction

TRAIL produces a lower average winner and a lower expectancy than a fixed 2.2R target, because break-even at 0.8R converts many trades that would reach 2.2R into scratch exits.

## Ablation command

From `fx_ai_engine/`:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m backtesting.run_replay --data-dir artifacts/datasets/<dataset> --variants baseline --exit-modes policy fixed_tp hybrid --costs base stress_1_5x
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant baseline --exit-mode policy
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant baseline --exit-mode fixed_tp
```

Also report `avg_winner_r` for each arm against the ADR 0001 D2 criterion (average winner ≥ 1.5R). `hybrid` is reported for information and does not enter the rule.

## Pre-registered rejection rule

Reject TRAIL (`policy` exit mode) if, on the development section, either:

- `policy` `expectancy_r` is lower than `fixed_tp` at both `base` and `stress_1_5x`; or
- `policy` `expectancy_r` is ≤ 0 at `stress_1_5x`.

Inconclusive if either arm has fewer than 100 development trades or either arm's R2 gate verdict is not `PASS`.

## Result

(empty until both arms pass the R2 gate)
