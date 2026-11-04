# Hypothesis cards (R6)

One card per loosened active parameter named in `docs/2026-11-03-system-assessment-and-benchmark.md` §4.1, and (since stage D, ADR 0014) one card per new strategy hypothesis. A card states what the parameter or strategy is believed to do, the run that tests it, and the result that would reject it. Cards are written before any ablation or replay runs.

| Card | Parameter | Active | Coded default | Variant |
| --- | --- | --- | --- | --- |
| [H-001](H-001-pullback-buffer-20.md) | `pullback_buffer_pips` | 20 | 2 | `pullback_2` |
| [H-002](H-002-rsi-bounds-open.md) | RSI bounds | 0–100 | 40–65 buy, 35–60 sell | `rsi_bounded` |
| [H-003](H-003-adx-gates-15-20.md) | ADX gates | 15 / 20 | 20 / 25 | `adx_20_25` |
| [H-004](H-004-trend-distance-0002.md) | `trend_distance_threshold` | 0.0002 | 0.0005 | `trend_distance_5pip` |
| [H-005](H-005-trail-exit-vs-2-2rr.md) | exit mode | TRAIL | nominal 2.2 R:R | `--exit-modes policy fixed_tp hybrid` |

Variants are defined in `backtesting/run_replay.py` (`VARIANTS`).

Strategy hypotheses (Phase 2b, owner-approved 2026-11-04, family size k = 3, Bonferroni z = 2.39; design `docs/plans/2026-11-04-stage-d-strategy-plugin-design.md`). H-008 `range_fade_v1` was dropped by the owner; the ID stays unused.

| Card | Strategy | Module | SRS fit | Live eligible |
| --- | --- | --- | --- | --- |
| [H-006](H-006-london-breakout.md) | `london_breakout_v1` | `core/strategy/london_breakout.py` | within SRS v1 | yes (not selected; live runs `trend_pullback_v1`) |
| [H-007](H-007-h1-channel-breakout.md) | `h1_channel_breakout_v1` | `core/strategy/h1_channel_breakout.py` | within SRS v1 | yes (not selected; live runs `trend_pullback_v1`) |
| [H-009](H-009-d1-breakout.md) | `d1_breakout_v1` | `core/strategy/d1_breakout.py` | needs SRS v1.x revision (§5.2, §5.3) | **no**, replay/research only (code-enforced) |

Strategies are selected with `run_replay --strategy <id>` (registry `core/strategy/registry.py`); a strategy card's comparator is the `trend_pullback_v1` baseline plus the RANDOM and INVERTED controls from `edge_diagnostics --strategy <id>`.

## Rules

1. Write the card before any ablation runs. Record the date.
2. Never edit the sections above "Result" after a run. If the hypothesis changes, write a new card with a new ID.
3. A result counts only when both arms have an R2 authenticity gate verdict of `PASS` (`research.authenticity_gate`), with a `policy_hash` and dataset hashes that match the replay report.
   Note (stage D): R2 includes the blocking `R4_COST_STRESS` check (expectancy at 2x costs > 0), so the losing baseline (−0.35R) can never PASS, and under this rule H-001 to H-005 can never produce a result. The strategy cards H-006, H-007 and H-009 therefore require PASS for the strategy arm only and, for the baseline and control rows, only the authenticity checks (R1A, R1B, R1C parity and the three R2 fill checks). The testing wave should apply the same rule to H-001 to H-005 through a change to this rule, recorded in those cards' history.
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

Strategy card template (stage D): "Parameter / Active / Coded default" become "Strategy / Frozen parameters / Comparators / SRS fit", and "Owner's original motivation" becomes "Proposed by / approved". Add "Family size k" with its Bonferroni z. Rules 1–5 apply unchanged (rule 3 as noted above).

```markdown
# H-00N: <strategy description> vs trend_pullback_v1

- ID: H-00N
- Strategy: `<strategy_id>`, `fx_ai_engine/core/strategy/<module>.py`
- Frozen parameters: <rules and the Params defaults>
- Comparators: `trend_pullback_v1` baseline; RANDOM and INVERTED controls at H-00N entry times
- SRS fit: <within SRS v1 | needs SRS v1.x revision; registered live_eligible=False>
- Proposed by / approved: <design, date> / <owner, date>
- Family size k: <k>, Bonferroni z = <z>
- Status: pre-registered, not run
- Date written:

## Mechanism
## Prediction
## Evaluation commands
## Pre-registered rejection rule
## Result
(empty until the arm passes the R2 gate)
```
