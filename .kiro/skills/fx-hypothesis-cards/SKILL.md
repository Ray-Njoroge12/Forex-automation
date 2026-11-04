---
name: fx-hypothesis-cards
description: Write or run an R6 hypothesis card (fx_ai_engine/docs/research/hypotheses/) before ablating a strategy parameter; use it when someone proposes changing or testing an agent threshold, exit mode or other strategy setting.
---

# FX hypothesis cards (R6)

## When to run

- Before any ablation of a strategy parameter: write the card first, run second.
- When recording an ablation result: fill only the card's Result section.
- When someone wants to loosen or tighten a threshold: that needs a card.

## Commands

Cards live in `fx_ai_engine/docs/research/hypotheses/`; the template, rules and common rejection rule are in its `README.md`. Existing cards: H-001 pullback buffer, H-002 RSI bounds, H-003 ADX gates, H-004 trend distance, H-005 TRAIL vs 2.2 R:R.

An ablation, run from `fx_ai_engine/`:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m backtesting.run_replay --data-dir artifacts/datasets/<dataset> --variants baseline <variant> --costs base stress_1_5x
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant baseline
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --data-dir artifacts/datasets/<dataset> --variant <variant>
```

Variants are the keys of `backtesting/run_replay.py` `VARIANTS`. Exit-mode ablations use `--exit-modes policy fixed_tp hybrid` and the gate's `--exit-mode`.

## How to read and fill a card

Fields: ID; parameter; active value and its source in `config_microcapital.py`; coded default with file and line; owner's original motivation; status; date; Mechanism; Prediction; Ablation command; Pre-registered rejection rule; Result.

- Owner's motivation is "unknown, owner to fill" until the owner answers assessment §7 question 4. Never infer or invent one.
- Status moves from `pre-registered, not run` to `run: rejected`, `run: not rejected` or `run: inconclusive`.
- The Result counts only if both arms' `verdict.json` show `verdict: PASS` with hashes matching the replay reports. Record the run id, `policy_hash`, dev trades, `expectancy_r` at `base` and `stress_1_5x` for each arm, and the decision.
- Use the development section only. Read the holdout once, after the decision is written.
- Fewer than 100 development trades in either arm means `inconclusive`.

Common rejection rule: reject the active value if its dev `expectancy_r` is lower than the coded-default arm's at both `base` and `stress_1_5x`, or is ≤ 0 at `stress_1_5x`.

## FX/SRS specifics

- Six majors. Thresholds in price units behave differently on JPY pairs (H-004); ask for the by-symbol breakdown.
- Completed bars only; replay and live share the agent code, so a card's mechanism must cite the agent line it relies on.
- Cards cover research overrides under `AGENT_THRESHOLDS`, exit modes and, since stage D, whole strategies registered in `core/strategy/` (H-006, H-007, H-009; `run_replay --strategy <id>`, template in the README). Locked SRS constants are read from `config_microcapital.py` and are not ablated or changed through cards.
- Always `USE_MT5_MOCK=1`. Never `main.py --mode demo`, never an MT5 `trade_*` tool, never the terminal bridge folder.

## Pitfalls

- Editing the Mechanism, Prediction or rejection rule after seeing results voids the card. Write a new card instead.
- Every run, including rejected arms, is appended to `artifacts/experiments/registry.jsonl`; count them when judging multiple testing.
- A card result never changes `config_microcapital.py` by itself; that is the owner's decision (protected path).

## Inspiration

Ideas, not text, from [Kryhr/26-quant-trading-skills](https://github.com/Kryhr/26-quant-trading-skills) (`hypothesis-driven-strategy-design`).
