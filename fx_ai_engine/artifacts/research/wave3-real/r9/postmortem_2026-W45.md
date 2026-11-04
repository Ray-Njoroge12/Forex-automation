# Trade postmortem 2026-W45 (R9)

Report only: no halt or veto authority; nothing in the engine reads this output, and trading behaviour is unchanged.

Rules-based and deterministic classification of closed demo trades by probable cause, from recorded data only (no LLM, no network). A cause is a hypothesis for a human to check.

## Data sources

| source | path | sha256_16 |
| --- | --- | --- |
| state DB (read-only, max trades.id 23, stream runtime_mt5_core_srs) | `../tmp/wave3-real/trading_state_snapshot.db` | 755a1809d8d8f928 |
| deals.json copy | `artifacts/research/wave3-real/inputs/deals.json` | 53d74b95df197c5f |
| M15 bars (6 files) | `artifacts/research/wave3-real/inputs` | 99181ccfbabb4765 |
| exit files (1) | `artifacts/research/wave3-real/inputs/exits` | 6789181e812096c6 |
| feedback files (11) | `artifacts/demo-evidence/2026-11-03/run_160754` | d0035a2618ac858c |
| economic calendar (31 events) | `/Users/raynj/Desktop/Forex-automation/.worktrees/validation-wave3/fx_ai_engine/data/economic_calendar.json` | 702b077b9758e135 |
| thresholds | `research/live_vs_replay_thresholds.json` | 04b13538c8afd78e |

Thresholds: version 1, sha256_16 04b13538c8afd78e: execution_entry_adverse_pips=1.0, execution_exit_adverse_pips=1.0, execution_latency_seconds=60, news_before_entry_minutes=30, news_after_exit_minutes=15, cost_r_fraction=0.25, regime_adx_margin=2.0, min_trades_for_pattern=10.

Precedence of the primary cause: EXECUTION > NEWS_CALENDAR > SPREAD_COST > REGIME_MISMATCH > ORDINARY_NOISE (ORDINARY_NOISE: outcome consistent with the model; nothing abnormal recorded). All flags are kept.

Week 2026-W45: exit times in [2026-11-02T00:00:00+00:00, 2026-11-09T00:00:00+00:00).

**INCONCLUSIVE: 1 closed trades; no pattern claims**

## Primary cause counts

| cause | trades |
| --- | --- |
| EXECUTION | 1 |
| NEWS_CALENDAR | 0 |
| SPREAD_COST | 0 |
| REGIME_MISMATCH | 0 |
| ORDINARY_NOISE | 0 |

## Trades

| trade_id | symbol | direction | entry UTC | exit UTC | net R | primary cause | flags (evidence) | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AI_20261103_163004_0564f3 | GBPUSD | BUY | 2026-11-03 16:30:05 | 2026-11-03 19:03:16 | +0.493 | EXECUTION | EXECUTION: exit deal not placed by the EA (magic 0, no sl/tp tag): possible manual close |  |

## Decision funnel rejections (stage, reason_code)

| stage | reason_code | count |
| --- | --- | --- |
| REGIME | REGIME_NO_TRADE | 32 |
| REGIME | REGIME_TRANSITION | 26 |
| TECHNICAL | TECH_CANDLE_CONFIRMATION_FAILED | 12 |
| PORTFOLIO | PM_SYMBOL_ALREADY_OPEN | 11 |
| PORTFOLIO | PM_EXPOSURE_LIMIT | 10 |
| TECHNICAL | TECH_RR_BELOW_MIN | 4 |
| NEWS | NEWS_BLACKOUT | 2 |
| TECHNICAL | TECH_MARKET_DATA_UNAVAILABLE | 2 |
| REGIME | REGIME_DATA_UNAVAILABLE | 1 |
| RESERVATION | RESERVATION_MAX_EXPOSURE | 1 |
