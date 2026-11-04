# Fill reconciliation (R7)

Report only: no halt or veto authority; the cost model is unchanged.

Offline comparison of demo broker fills with the replay cost and fill model (backtesting.unified_replay). Positive differences are worse for the trader than the model.

Cost scenario `base` (slippage 0.20 pip, commission 7.00 USD/lot round trip). Entry deals 2, comparable 1, not comparable NO_STRATEGY_SIGNAL 1.

**Verdict: INCONCLUSIVE** (per metric: INCONCLUSIVE below 20 comparable deals; SYSTEMATIC_BIAS when the 95% bootstrap CI of the mean excludes 0).

| metric | n | mean | median | std | p5 | p95 | CI of mean | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| fill_diff_pips | 1 | -1.100 | -1.100 | 0.000 | -1.100 | -1.100 | [-1.100, -1.100] | INCONCLUSIVE |
| spread_diff_pips | 1 | -1.100 | -1.100 | 0.000 | -1.100 | -1.100 | [-1.100, -1.100] | INCONCLUSIVE |
| slippage_diff_pips | 1 | +0.000 | +0.000 | 0.000 | +0.000 | +0.000 | [+0.000, +0.000] | INCONCLUSIVE |
| exit_slippage_diff_pips | 0 | n/a | n/a | n/a | n/a | n/a | n/a | INCONCLUSIVE |
| fill_latency_s | 1 | +5.422 | +5.422 | 0.000 | +5.422 | +5.422 | [+5.422, +5.422] | INCONCLUSIVE |
| commission_diff_usd_per_lot | 1 | -7.000 | -7.000 | 0.000 | -7.000 | -7.000 | [-7.000, -7.000] | INCONCLUSIVE |

Per symbol (mean, n, status):

| symbol | fill_diff_pips | spread_diff_pips | slippage_diff_pips | exit_slippage_diff_pips | fill_latency_s | commission_diff_usd_per_lot |
| --- | --- | --- | --- | --- | --- | --- |
| GBPUSD | -1.100 (n=1, INCONCLUSIVE) | -1.100 (n=1, INCONCLUSIVE) | +0.000 (n=1, INCONCLUSIVE) | n/a (n=0, INCONCLUSIVE) | +5.422 (n=1, INCONCLUSIVE) | -7.000 (n=1, INCONCLUSIVE) |

Cost ratio live/model (Phase 6 criterion: within 1.5x): mean 0.143 over 1 deals, INCONCLUSIVE.
Strict reference (market_entry, next bar after the decision): mean diff +0.900 pip over 1 deals; informational only.

## Suggested cost-model adjustment (data only; unified_replay.COST_SCENARIOS is not changed by this tool)

Actionable: False. MetaQuotes-Demo spreads and fills differ from a funded broker's (plan Phase 6); re-measure on the funded account before using any suggestion.

| parameter | current | suggested |
| --- | --- | --- |
| slippage_pips | 0.2 | 0.2000 |
| commission_per_lot_usd | 7.0 | 0.0000 |
| spread_floor_pips GBPUSD (p75 of actual) | 1.2 | 0.1000 |

## Not comparable

| trade_id | deal | symbol | reason |
| --- | --- | --- | --- |
| DEMO_MATRIX_VALID | 58758036737 | EURUSD | NO_STRATEGY_SIGNAL |

## Deals

| trade_id | symbol | dir | fill latency s | spread diff | slippage diff | fill diff | exit | exit slip diff | cost ratio |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| DEMO_MATRIX_VALID | EURUSD | BUY | n/a | n/a | n/a | n/a | STOP | -0.200 | n/a |
| AI_20261103_163004_0564f3 | GBPUSD | BUY | 5.4 | -1.100 | +0.000 | -1.100 | NOT_BY_EA | n/a | 0.143 |
