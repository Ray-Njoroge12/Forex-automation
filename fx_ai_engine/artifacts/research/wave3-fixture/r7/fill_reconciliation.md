# Fill reconciliation (R7)

Report only: no halt or veto authority; the cost model is unchanged.

Offline comparison of demo broker fills with the replay cost and fill model (backtesting.unified_replay). Positive differences are worse for the trader than the model.

Cost scenario `base` (slippage 0.20 pip, commission 7.00 USD/lot round trip). Entry deals 37, comparable 36, not comparable NO_DEAL 1, NO_STRATEGY_SIGNAL 1.

**Verdict: SYSTEMATIC_BIAS** (per metric: INCONCLUSIVE below 20 comparable deals; SYSTEMATIC_BIAS when the 95% bootstrap CI of the mean excludes 0; the verdict is SYSTEMATIC_BIAS if any of fill_diff_pips, spread_diff_pips, slippage_diff_pips, exit_slippage_diff_pips, commission_diff_usd_per_lot has it). Informational, not in the verdict: fill_latency_s (the replay assumes a fill at the bar close, 0 s, so fill latency is positive by construction; read its size).

| metric | n | mean | median | std | p5 | p95 | CI of mean | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| fill_diff_pips | 36 | +0.300 | +0.100 | 0.257 | +0.100 | +0.700 | [+0.219, +0.386] | SYSTEMATIC_BIAS |
| spread_diff_pips | 36 | +0.492 | +0.500 | 0.073 | +0.400 | +0.600 | [+0.467, +0.514] | SYSTEMATIC_BIAS |
| slippage_diff_pips | 36 | +0.100 | +0.100 | 0.000 | +0.100 | +0.100 | [+0.100, +0.100] | SYSTEMATIC_BIAS |
| exit_slippage_diff_pips | 34 | +0.129 | +0.200 | 0.097 | +0.000 | +0.200 | [+0.094, +0.159] | SYSTEMATIC_BIAS |
| fill_latency_s | 36 | +9.167 | +6.000 | 19.000 | +6.000 | +6.000 | [+6.000, +15.500] | SYSTEMATIC_BIAS (informational) |
| commission_diff_usd_per_lot | 35 | +0.001 | +0.000 | 0.004 | -0.007 | +0.008 | [-0.001, +0.002] | NO_SYSTEMATIC_BIAS |

Per symbol (mean, n, status):

| symbol | fill_diff_pips | spread_diff_pips | slippage_diff_pips | exit_slippage_diff_pips | fill_latency_s | commission_diff_usd_per_lot |
| --- | --- | --- | --- | --- | --- | --- |
| EURUSD | +0.344 (n=18, INCONCLUSIVE) | +0.489 (n=18, INCONCLUSIVE) | +0.100 (n=18, INCONCLUSIVE) | +0.153 (n=17, INCONCLUSIVE) | +6.000 (n=18, INCONCLUSIVE) | +0.002 (n=17, INCONCLUSIVE) |
| GBPUSD | +0.256 (n=18, INCONCLUSIVE) | +0.494 (n=18, INCONCLUSIVE) | +0.100 (n=18, INCONCLUSIVE) | +0.106 (n=17, INCONCLUSIVE) | +12.333 (n=18, INCONCLUSIVE) | -0.000 (n=18, INCONCLUSIVE) |

Cost ratio live/model (Phase 6 criterion: within 1.5x): mean 1.356 over 35 deals, WITHIN_LIMIT.
Strict reference (market_entry, next bar after the decision): mean diff +0.547 pip over 36 deals; informational only.

## Suggested cost-model adjustment (data only; unified_replay.COST_SCENARIOS is not changed by this tool)

Actionable: True. MetaQuotes-Demo spreads and fills differ from a funded broker's (plan Phase 6); re-measure on the funded account before using any suggestion.

| parameter | current | suggested |
| --- | --- | --- |
| slippage_pips | 0.2 | 0.3000 |
| commission_per_lot_usd | 7.0 | 7.0009 |
| spread_floor_pips EURUSD (p75 of actual) | 0.8 | 1.3000 |
| spread_floor_pips GBPUSD (p75 of actual) | 1.2 | 1.7750 |

## Not comparable

| trade_id | deal | symbol | reason |
| --- | --- | --- | --- |
| SYN_MATRIX | 58800000074 | GBPUSD | NO_STRATEGY_SIGNAL |
| SYN_MISSING |  | EURUSD | NO_DEAL |

## Deals

| trade_id | symbol | dir | fill latency s | spread diff | slippage diff | fill diff | exit | exit slip diff | cost ratio |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AI_SYN_000 | EURUSD | SELL | 6.0 | +0.500 | +0.100 | +0.100 | TARGET | +0.000 | 1.353 |
| AI_SYN_001 | GBPUSD | SELL | 6.0 | +0.500 | +0.100 | +0.100 | STOP | +0.200 | 1.348 |
| AI_SYN_002 | EURUSD | SELL | 6.0 | +0.400 | +0.100 | +0.100 | TARGET | +0.000 | 1.294 |
| AI_SYN_003 | GBPUSD | SELL | 6.0 | +0.400 | +0.100 | +0.100 | STOP | +0.200 | 1.304 |
| AI_SYN_004 | EURUSD | BUY | 6.0 | +0.400 | +0.100 | +0.500 | STOP | +0.200 | 1.369 |
| SYN_EXEC | GBPUSD | BUY | 120.0 | +0.600 | +0.100 | +0.700 | STOP | +0.200 | 1.391 |
| AI_SYN_006 | EURUSD | SELL | 6.0 | +0.500 | +0.100 | +0.100 | STOP | +0.200 | 1.421 |
| AI_SYN_007 | GBPUSD | BUY | 6.0 | +0.500 | +0.100 | +0.600 | STOP | +0.200 | 1.348 |
| AI_SYN_008 | EURUSD | SELL | 6.0 | +0.400 | +0.100 | +0.100 | TARGET | +0.000 | 1.294 |
| AI_SYN_009 | GBPUSD | SELL | 6.0 | +0.500 | +0.100 | +0.100 | TARGET | +0.000 | 1.286 |
| SYN_NEWS | EURUSD | BUY | 6.0 | +0.500 | +0.100 | +0.600 | STOP | +0.200 | 1.421 |
| AI_SYN_011 | GBPUSD | SELL | 6.0 | +0.600 | +0.100 | +0.100 | STOP | +0.200 | 1.391 |
| AI_SYN_012 | EURUSD | BUY | 6.0 | +0.600 | +0.100 | +0.700 | STOP | +0.200 | 1.474 |
| AI_SYN_013 | GBPUSD | SELL | 6.0 | +0.400 | +0.100 | +0.100 | TARGET | +0.000 | 1.238 |
| AI_SYN_014 | EURUSD | BUY | 6.0 | +0.500 | +0.100 | +0.600 | STOP | +0.200 | 1.421 |
| AI_SYN_015 | GBPUSD | SELL | 6.0 | +0.600 | +0.100 | +0.100 | TARGET | +0.000 | 1.333 |
| AI_SYN_016 | EURUSD | SELL | 6.0 | +0.600 | +0.100 | +0.100 | STOP | +0.200 | 1.474 |
| SYN_COST | GBPUSD | SELL | 6.0 | +0.500 | +0.100 | +0.100 | STOP | +0.200 | 1.348 |
| AI_SYN_018 | EURUSD | SELL | 6.0 | +0.600 | +0.100 | +0.100 | STOP | +0.200 | 1.474 |
| AI_SYN_019 | GBPUSD | SELL | 6.0 | +0.500 | +0.100 | +0.100 | TARGET | +0.000 | 1.286 |
| AI_SYN_020 | EURUSD | BUY | 6.0 | +0.500 | +0.100 | +0.600 | STOP | +0.200 | 1.421 |
| AI_SYN_021 | GBPUSD | SELL | 6.0 | +0.400 | +0.100 | +0.100 | TARGET | +0.000 | 1.238 |
| SYN_REGIME | EURUSD | SELL | 6.0 | +0.400 | +0.100 | +0.100 | STOP | +0.200 | 1.369 |
| AI_SYN_023 | GBPUSD | SELL | 6.0 | +0.400 | +0.100 | +0.100 | STOP | +0.200 | 1.304 |
| AI_SYN_024 | EURUSD | SELL | 6.0 | +0.500 | +0.100 | +0.100 | STOP | +0.200 | 1.421 |
| AI_SYN_025 | GBPUSD | BUY | 6.0 | +0.600 | +0.100 | +0.700 | TARGET | +0.000 | 1.333 |
| AI_SYN_026 | EURUSD | SELL | 6.0 | +0.500 | +0.100 | +0.100 | TARGET | +0.000 | 1.353 |
| AI_SYN_027 | GBPUSD | SELL | 6.0 | +0.400 | +0.100 | +0.100 | STOP | +0.200 | 1.304 |
| AI_SYN_028 | EURUSD | BUY | 6.0 | +0.500 | +0.100 | +0.600 | STOP | +0.200 | 1.421 |
| SYN_MANUAL | GBPUSD | BUY | 6.0 | +0.500 | +0.100 | +0.600 | NOT_BY_EA | n/a | 1.286 |
| AI_SYN_030 | EURUSD | BUY | 6.0 | +0.500 | +0.100 | +0.600 | STOP | +0.200 | 1.421 |
| AI_SYN_031 | GBPUSD | SELL | 6.0 | +0.400 | +0.100 | +0.100 | TARGET | +0.000 | 1.238 |
| AI_SYN_033 | GBPUSD | BUY | 6.0 | +0.600 | +0.100 | +0.700 | STOP | +0.200 | 1.391 |
| AI_SYN_034 | EURUSD | BUY | 6.0 | +0.500 | +0.100 | +0.600 | STOP | +0.200 | 1.421 |
| AI_SYN_035 | GBPUSD | SELL | 6.0 | +0.500 | +0.100 | +0.100 | TARGET | +0.000 | 1.285 |
| SYN_OPEN | EURUSD | BUY | 6.0 | +0.400 | +0.100 | +0.500 | open | n/a | n/a |
| SYN_MATRIX | GBPUSD | BUY | n/a | n/a | n/a | n/a | STOP | -0.200 | n/a |
