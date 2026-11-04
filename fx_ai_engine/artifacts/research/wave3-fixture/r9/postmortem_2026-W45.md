# Trade postmortem 2026-W45 (R9)

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

Week 2026-W45: exit times in [2026-11-02T00:00:00+00:00, 2026-11-09T00:00:00+00:00).

**20 closed trades (>= min_trades_for_pattern 10): causes with >= 25% share listed**

## Primary cause counts

| cause | trades |
| --- | --- |
| EXECUTION | 1 |
| NEWS_CALENDAR | 1 |
| SPREAD_COST | 1 |
| REGIME_MISMATCH | 0 |
| ORDINARY_NOISE | 17 |

## Patterns (causes with >= 25% of the week's trades)

- ORDINARY_NOISE: 17/20 (85%)

## Trades

| trade_id | symbol | direction | entry UTC | exit UTC | net R | primary cause | flags (evidence) | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AI_SYN_000 | EURUSD | SELL | 2026-11-02 07:00:06 | 2026-11-02 07:33:14 | +2.200 | ORDINARY_NOISE | none |  |
| AI_SYN_001 | GBPUSD | SELL | 2026-11-02 10:00:06 | 2026-11-02 12:20:36 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_002 | EURUSD | SELL | 2026-11-02 13:00:06 | 2026-11-02 14:53:28 | +2.200 | ORDINARY_NOISE | none |  |
| AI_SYN_003 | GBPUSD | SELL | 2026-11-02 16:00:06 | 2026-11-02 18:07:01 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_004 | EURUSD | BUY | 2026-11-03 07:00:06 | 2026-11-03 08:46:18 | -1.000 | ORDINARY_NOISE | none |  |
| SYN_EXEC | GBPUSD | BUY | 2026-11-03 10:02:00 | 2026-11-03 10:36:48 | -1.000 | EXECUTION | EXECUTION: entry deal 120.0 s after bar close (limit 60 s) |  |
| AI_SYN_006 | EURUSD | SELL | 2026-11-03 13:00:06 | 2026-11-03 14:00:08 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_007 | GBPUSD | BUY | 2026-11-03 16:00:06 | 2026-11-03 17:19:52 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_008 | EURUSD | SELL | 2026-11-04 07:00:06 | 2026-11-04 08:21:42 | +2.200 | ORDINARY_NOISE | none |  |
| AI_SYN_009 | GBPUSD | SELL | 2026-11-04 10:00:06 | 2026-11-04 11:45:29 | +2.200 | ORDINARY_NOISE | none |  |
| SYN_NEWS | EURUSD | BUY | 2026-11-04 13:00:06 | 2026-11-04 14:50:01 | -1.000 | NEWS_CALENDAR | NEWS_CALENDAR: high-impact USD 'Synthetic high-impact release' at 2026-11-04T13:10:00+00:00 (+10 min from entry) |  |
| AI_SYN_011 | GBPUSD | SELL | 2026-11-04 16:00:06 | 2026-11-04 18:17:34 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_012 | EURUSD | BUY | 2026-11-05 07:00:06 | 2026-11-05 07:48:33 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_013 | GBPUSD | SELL | 2026-11-05 10:00:06 | 2026-11-05 11:20:24 | +2.200 | ORDINARY_NOISE | none |  |
| AI_SYN_014 | EURUSD | BUY | 2026-11-05 13:00:06 | 2026-11-05 14:36:30 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_015 | GBPUSD | SELL | 2026-11-05 16:00:06 | 2026-11-05 17:06:47 | +2.200 | ORDINARY_NOISE | none |  |
| AI_SYN_016 | EURUSD | SELL | 2026-11-06 07:00:06 | 2026-11-06 08:54:47 | -1.000 | ORDINARY_NOISE | none |  |
| SYN_COST | GBPUSD | SELL | 2026-11-06 10:00:06 | 2026-11-06 11:39:59 | -1.000 | SPREAD_COST | SPREAD_COST: costs 1.033 R >= 0.25 R: spread 1.70 + slippage 0.30 + exit slippage 0.40 pip over a 3-pip stop (funnel TECHNICAL stop_pips) + commission 74.68 USD / risk 320.00 USD |  |
| AI_SYN_018 | EURUSD | SELL | 2026-11-06 13:00:06 | 2026-11-06 14:54:55 | -1.000 | ORDINARY_NOISE | none |  |
| AI_SYN_019 | GBPUSD | SELL | 2026-11-06 16:00:06 | 2026-11-06 16:49:37 | +2.200 | ORDINARY_NOISE | none |  |

## Decision funnel rejections (stage, reason_code)

No REJECT rows in the decision funnel for this period.
