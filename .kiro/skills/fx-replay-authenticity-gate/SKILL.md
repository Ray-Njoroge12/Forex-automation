---
name: fx-replay-authenticity-gate
description: Run the R2 replay authenticity gate (research.authenticity_gate) before describing any unified-replay result as evidence; use it when a replay report, ablation or hypothesis card result is about to be quoted, or when fills look too good.
---

# FX replay authenticity gate (R2)

## When to run

- Before any replay result is quoted as evidence: in a report, an ADR, a hypothesis card's Result section, or a D3 risk decision.
- For each arm of an ablation (each `--variant` and each `--exit-mode`).
- After changes to `backtesting/unified_replay.py`, `replay_execution.py`, the cost model or the agents.

## Commands

Run from `fx_ai_engine/`:

```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate --bridge-dir tests/fixtures/live_bars_2026-11-03 --max-decisions 60 --seed 7 --out artifacts/research/fixture-demo/r2
```

On a history export: `--data-dir artifacts/datasets/<dataset> --variant <arm> --exit-mode policy|fixed_tp|hybrid --cost base|stress_1_5x|stress_2x`. `--min-breakeven-multiple` (default 2.0) sets the cost-stress bar; `--start/--end` limit the window. The CLI runs the replay in process (same two stages as `run_replay`), then R1, the fill checks and R4.

Exit code 0 means PASS, 1 means FAIL. In code, `research.authenticity_gate.audit_replay(...)` gates a result you already have.

## How to read the output

`verdict.json`:

- `verdict`: `PASS` or `FAIL`; `evidence_status`: `EVIDENCE_ELIGIBLE` or `NOT_EVIDENCE`.
- `reasons`: blocking checks that did not pass; `warnings`: non-blocking ones.
- `checks[]` in fixed order, each with `id`, `status`, `blocking`, `detail` (up to 20 offenders):
  1. `R1A_TRUNCATION`, 2. `R1B_FUTURE_CORRUPTION`, 3. `R1C_WARMUP_PARITY` (from R1);
  4. `R1C_WARMUP_DEVIATION`, non-blocking, because live and replay share the same windows;
  5. `R2_NO_SAME_BAR_FILL`: the entry is the open of the first M15 bar at or after the decision, priced BUY `bid_open + spread + slippage`, SELL `bid_open - slippage`;
  6. `R2_FILLS_WITHIN_BID_ASK`: entry, partial and exit prices lie in the bar's modelled bid or ask range (ask = bid + spread); a conservative fill outside the range is still a failure;
  7. `R2_NO_FILL_IN_GAP_OR_CLOSED_MARKET`: the fill bars exist and are outside Friday 17:00 to Sunday 17:00 New York; `entries_after_data_gap` is reported;
  8. `R4_COST_STRESS`: fixed-trade-set expectancy above 0 at 2× base spread+slippage (commission at 1×). The multiple is absolute: with `--cost stress_1_5x` the trades are re-costed from 1.5× to 2×, not from 1× to 2×; `detail[0].replay_cost_multiplier` records the starting point.
- `policy_hash`, `dataset_sha256_16`, `variant`, `exit_mode`, `cost` bind the verdict to one replay.

`gate_report.md` starts with `# Replay authenticity gate: PASS` or `# NOT EVIDENCE: replay failed the authenticity gate`.

**Rule:** a replay report is evidence only if a `verdict.json` with `verdict: PASS` exists whose `policy_hash` and `dataset_sha256_16` match that report. Otherwise call it NOT EVIDENCE, whatever its expectancy.

## FX/SRS specifics

- Six majors; MetaQuotes-Demo BID bars; spreads are `max(recorded, floor)` per symbol (ADR 0001 D1: demo spreads may differ from a real broker).
- Completed bars only; entries fill at the next M15 open.
- Locked constants are read from `config_microcapital.py`, never typed or changed. The gate never changes trading behaviour.
- Always `USE_MT5_MOCK=1`. Never `main.py --mode demo`, never an MT5 `trade_*` tool, never the terminal bridge folder.

## Pitfalls

- The fixture verdict is FAIL (only `R4_COST_STRESS`, mean R < 0). That shows the gate works; it says nothing about the strategy.
- Re-running with a different `--max-decisions` or `--seed` changes the R1 sample; record both.
- Fill tolerances allow 5e-6 for prices printed with 5 decimals; do not widen them to make a run pass.
- `backtesting.run_replay` gates every result (ADR 0003): it writes `<stem>_gate.json`, puts the gate's first line at the top of the report, and adds `gate_verdict`/`evidence_status` to the registry row. R1 is computed once per policy hash and dataset hashes (60 decisions per symbol, seed 7) and cached under `artifacts/replay/cache/r1_bias_audit/`. `--no-gate` skips R1/R2 and labels the report `# NOT EVIDENCE: authenticity gate skipped (--no-gate)`.

## Inspiration

Ideas, not text, from [Kryhr/26-quant-trading-skills](https://github.com/Kryhr/26-quant-trading-skills) (`backtest-result-authenticity-audit`, `fill-feasibility-and-impossible-fill-detection`) and [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (MIT, "Time Machine" replay).
