# Hypothesis cards (R6)

One card per loosened active parameter named in `docs/2026-11-03-system-assessment-and-benchmark.md` §4.1. A card states what the parameter is believed to do, the ablation that tests it, and the result that would reject it. Cards are written before any ablation runs.

| Card | Parameter | Active | Coded default | Variant |
| --- | --- | --- | --- | --- |
| [H-001](H-001-pullback-buffer-20.md) | `pullback_buffer_pips` | 20 | 2 | `pullback_2` |
| [H-002](H-002-rsi-bounds-open.md) | RSI bounds | 0–100 | 40–65 buy, 35–60 sell | `rsi_bounded` |
| [H-003](H-003-adx-gates-15-20.md) | ADX gates | 15 / 20 | 20 / 25 | `adx_20_25` |
| [H-004](H-004-trend-distance-0002.md) | `trend_distance_threshold` | 0.0002 | 0.0005 | `trend_distance_5pip` |
| [H-005](H-005-trail-exit-vs-2-2rr.md) | exit mode | TRAIL | nominal 2.2 R:R | `--exit-modes policy fixed_tp hybrid` |

Variants are defined in `backtesting/run_replay.py` (`VARIANTS`).

## Rules

1. Write the card before any ablation runs. Record the date.
2. Never edit the sections above "Result" after a run. If the hypothesis changes, write a new card with a new ID.
3. A result counts only when both arms have an R2 authenticity gate verdict of `PASS` (`research.authenticity_gate`), with a `policy_hash` and dataset hashes that match the replay report.
4. Decide on the development section only. Read the holdout once, after the development decision is written into the card.
5. Fewer than 100 development trades in either arm means the result is `inconclusive`.
6. The owner's original motivation for each loosened value is unknown (assessment §7 question 4). Leave that field as "unknown, owner to fill" until the owner answers. Do not infer one.

## Common rejection rule

The loosened (active, `baseline`) value is rejected if either condition holds on the development section:

- the `baseline` `expectancy_r` is lower than the coded-default arm's at both `base` and `stress_1_5x`;
- the `baseline` `expectancy_r` is ≤ 0 at `stress_1_5x`.

## Card template

```markdown
# H-00N: <parameter> <active> vs <default>

- ID: H-00N
- Parameter:
- Active value: <value>, from `fx_ai_engine/config_microcapital.py` `_SHARED_POLICY["AGENT_THRESHOLDS"]`
- Coded default: <value>, from `<file>:<line>`
- Owner's original motivation: unknown, owner to fill (assessment §7 Q4)
- Status: pre-registered, not run
- Date written:

## Mechanism
## Prediction
## Ablation command
## Pre-registered rejection rule
## Result
(empty until both arms pass the R2 gate)
```
