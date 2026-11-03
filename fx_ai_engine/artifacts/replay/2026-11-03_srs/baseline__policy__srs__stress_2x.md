# Replay report: baseline__policy__srs / stress_2x

Generated 20261103T174558Z. Policy hash `f00f1f9c3e91`. Datasets: AUDUSD_D1.csv, AUDUSD_H1.csv, AUDUSD_H4.csv, AUDUSD_M15.csv, EURUSD_D1.csv, EURUSD_H1.csv, EURUSD_H4.csv, EURUSD_M15.csv, GBPUSD_D1.csv, GBPUSD_H1.csv, GBPUSD_H4.csv, GBPUSD_M15.csv, USDCAD_D1.csv, USDCAD_H1.csv, USDCAD_H4.csv, USDCAD_M15.csv, USDCHF_D1.csv, USDCHF_H1.csv, USDCHF_H4.csv, USDCHF_M15.csv, USDJPY_D1.csv, USDJPY_H1.csv, USDJPY_H4.csv, USDJPY_M15.csv.

Assumptions: completed bars only; next-M15-open market fills; BID bars with ask = bid + max(recorded, floor) spread x cost multiplier; slippage 0.2 pips and $7/lot round-trip commission x multiplier; stop assumed before target within a bar; EA break-even/partial/trail applied from bar extremes, effective next bar; no calendar blackout, swap, or partial fills; loss-streak halt cleared at the next ISO week (operator review); candidates generated once with base costs.

## Development (2022-12-24 to 2026-05-03)
- Trades 16, win rate 25.0% (SRS r>0: 31.2%), break-even 12.5%
- Expectancy -0.525R (95% CI -0.887 to -0.092), profit factor 0.2374, avg winner 0.71R, avg loser -1.12R
- Return -20.1%, CAGR -6.5%, max drawdown 20.3%, longest losing run 3
- Exits: {'STOP': 16}

### By symbol
| symbol | trades | win_rate | expectancy_r | profit_factor | net_usd |
| --- | --- | --- | --- | --- | --- |
| AUDUSD | 8 | 0.5 | -0.2164 | 0.5846 | -4305.78 |
| EURUSD | 4 | 0.0 | -0.9601 | 0.0 | -9747.96 |
| USDJPY | 4 | 0.0 | -0.7066 | 0.0297 | -6000.55 |

### By entry hour (UTC)
| hour | trades | win_rate | expectancy_r | profit_factor | net_usd |
| --- | --- | --- | --- | --- | --- |
| 10 | 1 | 1.0 | 0.9391 | inf | 1863.24 |
| 14 | 2 | 0.5 | -0.1676 | 0.5138 | -1528.45 |
| 15 | 2 | 0.0 | -0.9202 | 0.0 | -3164.69 |
| 16 | 1 | 0.0 | 0.0863 | inf | 183.77 |
| 17 | 1 | 0.0 | -1.1573 | 0.0 | -3074.33 |
| 7 | 5 | 0.2 | -0.8269 | 0.1593 | -10121.56 |
| 8 | 3 | 0.3333 | -0.2723 | 0.3796 | -1084.11 |
| 9 | 1 | 0.0 | -1.1395 | 0.0 | -3128.16 |

### Walk-forward windows (half-years)

| window | trades | win_rate | expectancy_r | profit_factor | net_usd |
| --- | --- | --- | --- | --- | --- |
| 2022H2 | 3 | 0.0 | -0.8833 | 0.0 | -7196.46 |
| 2023H1 | 13 | 0.3077 | -0.4422 | 0.3269 | -12857.83 |

### Monte Carlo (bootstrap of realised R, 30 trades, risk 3.2%)
P(max DD > 15%) 100%, P(max DD > 20%) 98%, median max DD 35.6%, P(below start) 100%

## Holdout (2026-05-03 to 2026-11-03)
No trades.

## Decision funnel
| outcome | count |
| --- | --- |
| APPROVED | 16 |
| EXECUTED | 16 |
| PORTFOLIO:PM_SYMBOL_ALREADY_OPEN | 64 |
| PORTFOLIO:PM_CORRELATION_BLOCK | 17 |
| HARD_RISK:RISK_LOSS_STREAK | 187 |
| ADVERSARIAL:ADV_RSI_EXHAUSTION_BUY | 526 |
| ADVERSARIAL:ADV_SPREAD_TOO_WIDE | 19562 |
| ADVERSARIAL:ADV_RSI_EXHAUSTION_SELL | 404 |
| PORTFOLIO:PM_EXPOSURE_LIMIT | 18 |
| HARD_RISK:RISK_DRAWDOWN_STOP | 25401 |

## Candidate funnel (stage 1)

- EURUSD: SESSION_INACTIVE=40140, REGIME:REGIME_TRANSITION=22139, CANDIDATE=8232, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=7909, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5571, REGIME:REGIME_NO_TRADE=5419
- GBPUSD: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=23156, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=8306, CANDIDATE=6881, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5166, REGIME:REGIME_NO_TRADE=4747
- USDJPY: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=24346, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=7448, CANDIDATE=6517, REGIME:REGIME_NO_TRADE=5682, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=4974
- AUDUSD: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=22419, REGIME:REGIME_NO_TRADE=8503, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=8352, CANDIDATE=8294, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=4819
- USDCAD: SESSION_INACTIVE=40141, REGIME:REGIME_TRANSITION=21790, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=8666, CANDIDATE=7993, REGIME:REGIME_NO_TRADE=6184, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5115
- USDCHF: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=22566, CANDIDATE=8278, REGIME:REGIME_NO_TRADE=7375, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=6746, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5234
