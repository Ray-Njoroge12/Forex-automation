---
name: fx-trade-postmortem
description: Run the R9 signal postmortem (research.trade_postmortem) to give each closed MetaTrader 5 demo trade a probable cause (execution, news/calendar, spread/cost, regime mismatch, or ordinary noise) with fixed rules over recorded data, and write one markdown report per ISO week. Deterministic, no LLM, report only.
---
# FX trade postmortem (R9)
## When to run
- Once a week during the demo burn-in (plan Phase 6), after the fill reconciliation (`fx-fill-reconciliation`) and the drift monitor (`fx-drift-monitor`).
- When a single losing trade gets attention and someone wants to know whether anything abnormal was recorded around it before blaming the strategy.
- When the drift monitor says `INVESTIGATE`: the weekly cause counts show where to look first.
## Commands
Run from `fx_ai_engine/`. Build the synthetic fixture first (30 ordinary trades plus one planted trade per cause, over ISO weeks 2026-W45 and 2026-W46):
```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.synthetic_demo --out ../tmp/wave3-fixture --seed 3
USE_MT5_MOCK=1 ../.venv/bin/python -m research.trade_postmortem --db ../tmp/wave3-fixture/trading_state.db --deals ../tmp/wave3-fixture/deals.json --bars-dir ../tmp/wave3-fixture/bars --exits-dir ../tmp/wave3-fixture/exits --feedback-dir ../tmp/wave3-fixture/feedback --calendar ../tmp/wave3-fixture/economic_calendar.json --out artifacts/research/wave3-fixture/r9
```
On real demo data, use copies only: a read-only snapshot of the state DB (`research.demo_records.snapshot_db` opens the live DB with `mode=ro`) and `cp` copies of `market/deals.json`, `market/bars_*_M15.json` and `exits/exit_*.json`:
```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.trade_postmortem --db ../tmp/wave3-real/trading_state_snapshot.db --deals artifacts/research/wave3-real/inputs/deals.json --bars-dir artifacts/research/wave3-real/inputs --exits-dir artifacts/research/wave3-real/inputs/exits --feedback-dir artifacts/demo-evidence/2026-11-03/run_160754 --out artifacts/research/wave3-real/r9
```
Options: `--week 2026-W45` (one week; default is every week with a closed trade), `--calendar` (default `data/economic_calendar.json`), `--feedback-dir` (EA `execution_*.json` copies), `--evidence-stream` (default `runtime_mt5_core_srs`), `--thresholds` (default `research/live_vs_replay_thresholds.json`). Outputs `postmortem_<week>.md` per week and `postmortem.json` (every week and trade, the thresholds used, and a metadata block holding the run time). With no closed trades it writes `postmortem_none.md`.
## The rules and where their data comes from
Every rule is evaluated for every trade and every flag is kept with its evidence string. Thresholds come from the `r9_postmortem` section of the thresholds file.
- `EXECUTION`: from the trade's R7 row (`research.fill_reconciliation.reconcile`, matched by trade_id): entry fill at least `execution_entry_adverse_pips` worse than the replay fill; entry deal at least `execution_latency_seconds` after the bar close; stop-exit slippage at least `execution_exit_adverse_pips` beyond the model; or an exit deal without the EA magic number and without an `[sl]`/`[tp]` tag (`NOT_BY_EA`, a possible manual close). From the DB: an unresolved row in `execution_feedback_conflicts`, or a reservation still in `EXECUTION_UNCERTAIN`.
- `NEWS_CALENDAR`: a high-impact event in the economic calendar for either currency of the pair between `news_before_entry_minutes` before entry and `news_after_exit_minutes` after exit. If that window is not inside the calendar's first-to-last event span, the trade gets a `NEWS_NOT_EVALUABLE` note, because a missing event cannot be told apart from a calendar gap.
- `SPREAD_COST`: costs in R = (actual spread + adverse entry slippage + adverse exit slippage) / stop pips, plus commission / risk in USD. Stop pips come from the trade's TECHNICAL PASS funnel row (`stop_pips=`), else `trades.stop_loss` (labelled as fallback). Risk in USD is `|profit_loss / r_multiple|` because the DB does not store it. Flags when costs reach `cost_r_fraction` R, or when a losing trade would have been positive before costs.
- `REGIME_MISMATCH` (losing trades only): the entry REGIME funnel row has ADX less than `regime_adx_margin` above the transition level, or a REGIME row for the same symbol during the trade shows a different regime.
- `INSUFFICIENT_DATA` note (not a cause): no R7 row, or an R7 row that is not comparable (no deal, no bar data), so the fill rules were skipped; or no entry REGIME row.
## Precedence
The primary cause is the first flag present in this order: `EXECUTION`, `NEWS_CALENDAR`, `SPREAD_COST`, `REGIME_MISMATCH`. With no flag the trade is `ORDINARY_NOISE`: the outcome fits the model and nothing abnormal was recorded. Execution comes first because a bad fill or a manual close makes every other reading of the trade unreliable. The same order is printed in each report and recorded in ADR 0004.
## Small samples
A week with fewer than `min_trades_for_pattern` closed trades carries the line `INCONCLUSIVE: <n> closed trades; no pattern claims` and no pattern section. Otherwise the report lists the causes that account for at least 25% of the week's trades. With the few real demo trades available, every real week is INCONCLUSIVE; report it that way.
## FX/SRS specifics
- Report only: no halt or veto authority. The SRS halts and limits stay with the Hard Risk Engine, and nothing in the engine reads this output.
- Deterministic and reproducible: no LLM calls, no network, no random numbers. Two runs on the same inputs give the same `postmortem.json` apart from the metadata run time, and each report lists its inputs with sha256 prefixes.
- Exit times come from `deals.json` (`time_msc`, UTC), and the ISO week (`%G-W%V`) is taken from the exit time. Exit files carry server time (UTC+2 on MetaQuotes-Demo).
- EA demo-matrix test deals have no trades row and are not classified.
- Always `USE_MT5_MOCK=1`. Never `main.py --mode demo`, never an MT5 `trade_*` tool, and never point `--out` into an `MQL5/Files/bridge` folder (the CLI refuses).
## Pitfalls
- The cause is a probable cause from recorded data, not proof. `ORDINARY_NOISE` on a loss means nothing abnormal was recorded; it does not mean nothing went wrong.
- An out-of-date calendar silently weakens the news rule. Check the `NEWS_NOT_EVALUABLE` notes and refresh `data/economic_calendar.json` before reading the news counts.
- Funnel rows are needed for stop pips and the regime rules. A DB snapshot taken before the engine logged the trade's funnel rows gives `INSUFFICIENT_DATA` notes.
- Do not tune the thresholds after reading a week's report. Add a new thresholds version with the reason in ADR 0004.
## Inspiration
Ideas, not text, from [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) (`signal-postmortem`).
