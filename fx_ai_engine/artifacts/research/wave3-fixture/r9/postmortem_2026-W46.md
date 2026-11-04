# Trade postmortem 2026-W46 (R9)

Report only: no halt or veto authority; nothing in the engine reads this output, and trading behaviour is unchanged.

Rules-based and deterministic classification of closed demo trades by probable cause, from recorded data only (no LLM, no network). A cause is a hypothesis for a human to check.

## Data sources

| source | path | sha256_16 |
| --- | --- | --- |
| state DB (read-only, max trades.id 37, stream runtime_mt5_core_srs) | `../tmp/wave3-fixture/trading_state.db` | 2b33691e9908c021 |
| deals.json copy | `../tmp/wave3-fixture/deals.json` | 7b019047b11014f3 |
| M15 bars (2 files) | `../tmp/wave3-fixture/bars` | be3b434c4d5dd63f |
| exit files (36) | `../tmp/wave3-fixture/exits` | a40561dbdd5ec5ac |
| feedback files (2) | `../tmp/wave3-fixture/feedback` | 38400ee84270fdb9 |
| economic calendar (3 events) | `../tmp/wave3-fixture/economic_calendar.json` | 46012925d0827304 |
| thresholds | `research/live_vs_replay_thresholds.json` | 04b13538c8afd78e |

Thresholds: version 1, sha256_16 04b13538c8afd78e: execution_entry_adverse_pips=1.0, execution_exit_adverse_pips=1.0, execution_latency_seconds=60, news_before_entry_minutes=30, news_after_exit_minutes=15, cost_r_fraction=0.25, regime_adx_margin=2.0, min_trades_for_pattern=10.

Precedence of the primary cause: EXECUTION > NEWS_CALENDAR > SPREAD_COST > REGIME_MISMATCH > ORDINARY_NOISE (ORDINARY_NOISE: outcome consistent with the model; nothing abnormal recorded). All flags are kept.

Week 2026-W46: exit times in [2026-11-09T00:00:00+00:00, 2026-11-16T00:00:00+00:00).

**16 closed trades (>= min_trades_for_pattern 10): causes with >= 25% share listed**

## Primary cause counts

| cause | trades |
| --- | --- |
| EXECUTION | 1 |
| NEWS_CALENDAR | 0 |
| SPREAD_COST | 0 |
| REGIME_MISMATCH | 1 |
| ORDINARY_NOISE | 14 |

## Patterns (causes with >= 25% of the week's trades)

- ORDINARY_NOISE: 14/16 (88%)

## Trades

| trade_id | symbol | direction | entry UTC | exit UTC | net R | primary cause | flags (evidence) | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AI_SYN_020 | EURUSD | BUY | 2026-11-09 07:00:06 | 2026-11-09 09:01:46 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_021 | GBPUSD | SELL | 2026-11-09 10:00:06 | 2026-11-09 11:17:39 | +2.200 | ORDINARY_NOISE | none |  |
| SYN_REGIME | EURUSD | SELL | 2026-11-09 13:00:06 | 2026-11-09 15:22:44 | -1.000 | REGIME_MISMATCH | REGIME_MISMATCH: regime changed during trade: TRENDING_BEAR -> TRANSITION at 2026-11-09T14:00:03+00:00 (REGIME_TRANSITION; 1 row(s)) |  |
| AI_SYN_023 | GBPUSD | SELL | 2026-11-09 16:00:06 | 2026-11-09 18:19:15 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_024 | EURUSD | SELL | 2026-11-10 07:00:06 | 2026-11-10 07:48:50 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_025 | GBPUSD | BUY | 2026-11-10 10:00:06 | 2026-11-10 10:35:39 | +2.200 | ORDINARY_NOISE | none |  |
| AI_SYN_026 | EURUSD | SELL | 2026-11-10 13:00:06 | 2026-11-10 14:36:33 | +2.200 | ORDINARY_NOISE | none |  |
| AI_SYN_027 | GBPUSD | SELL | 2026-11-10 16:00:06 | 2026-11-10 17:24:42 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_028 | EURUSD | BUY | 2026-11-11 07:00:06 | 2026-11-11 08:05:17 | -1.000 | ORDINARY_NOISE | none |  |
| SYN_MANUAL | GBPUSD | BUY | 2026-11-11 10:00:06 | 2026-11-11 12:08:49 | +0.500 | EXECUTION | EXECUTION: exit deal not placed by the EA (magic 0, no sl/tp tag): possible manual close |  |
| AI_SYN_030 | EURUSD | BUY | 2026-11-11 13:00:06 | 2026-11-11 14:38:16 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_031 | GBPUSD | SELL | 2026-11-11 16:00:06 | 2026-11-11 18:09:06 | +2.200 | ORDINARY_NOISE | none |  |
| SYN_MISSING | EURUSD | BUY | 2026-11-12 07:00:03 | 2026-11-12 07:38:30 | -1.000 | ORDINARY_NOISE | none | entry time = decision time (no deal, no execution_time); INSUFFICIENT_DATA: R7 row not comparable (NO_DEAL); fill-based rules not evaluated |
| AI_SYN_033 | GBPUSD | BUY | 2026-11-12 10:00:06 | 2026-11-12 10:48:46 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_034 | EURUSD | BUY | 2026-11-12 13:00:06 | 2026-11-12 14:23:50 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_035 | GBPUSD | SELL | 2026-11-12 16:00:06 | 2026-11-12 17:31:49 | +2.200 | ORDINARY_NOISE | none |  |

## Decision funnel rejections (stage, reason_code)

| stage | reason_code | count |
| --- | --- | --- |
| REGIME | REGIME_TRANSITION | 1 |
