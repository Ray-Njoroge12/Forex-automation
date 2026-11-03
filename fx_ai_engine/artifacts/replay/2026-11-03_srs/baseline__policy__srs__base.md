# Replay report: baseline__policy__srs / base

Generated 20261103T174558Z. Policy hash `f00f1f9c3e91`. Datasets: AUDUSD_D1.csv, AUDUSD_H1.csv, AUDUSD_H4.csv, AUDUSD_M15.csv, EURUSD_D1.csv, EURUSD_H1.csv, EURUSD_H4.csv, EURUSD_M15.csv, GBPUSD_D1.csv, GBPUSD_H1.csv, GBPUSD_H4.csv, GBPUSD_M15.csv, USDCAD_D1.csv, USDCAD_H1.csv, USDCAD_H4.csv, USDCAD_M15.csv, USDCHF_D1.csv, USDCHF_H1.csv, USDCHF_H4.csv, USDCHF_M15.csv, USDJPY_D1.csv, USDJPY_H1.csv, USDJPY_H4.csv, USDJPY_M15.csv.

Assumptions: completed bars only; next-M15-open market fills; BID bars with ask = bid + max(recorded, floor) spread x cost multiplier; slippage 0.2 pips and $7/lot round-trip commission x multiplier; stop assumed before target within a bar; EA break-even/partial/trail applied from bar extremes, effective next bar; no calendar blackout, swap, or partial fills; loss-streak halt cleared at the next ISO week (operator review); candidates generated once with base costs.

## Development (2022-12-24 to 2026-05-03)
- Trades 16, win rate 18.8% (SRS r>0: 37.5%), break-even 18.8%
- Expectancy -0.542R (95% CI -0.843 to -0.199), profit factor 0.1279, avg winner 0.52R, avg loser -1.03R
- Return -21.1%, CAGR -6.9%, max drawdown 21.1%, longest losing run 4
- Exits: {'STOP': 16}

### By symbol
| symbol | trades | win_rate | expectancy_r | profit_factor | net_usd |
| --- | --- | --- | --- | --- | --- |
| AUDUSD | 4 | 0.5 | -0.2511 | 0.4166 | -3172.04 |
| EURUSD | 7 | 0.0 | -0.6282 | 0.0066 | -12346.78 |
| USDCHF | 1 | 0.0 | -1.0678 | 0.0 | -1461.46 |
| USDJPY | 4 | 0.25 | -0.5506 | 0.1532 | -4167.96 |

### By entry hour (UTC)
| hour | trades | win_rate | expectancy_r | profit_factor | net_usd |
| --- | --- | --- | --- | --- | --- |
| 10 | 1 | 0.0 | -1.0668 | 0.0 | -3172.23 |
| 13 | 1 | 1.0 | 0.5607 | inf | 1208.06 |
| 15 | 4 | 0.0 | -0.9371 | 0.0 | -8230.53 |
| 16 | 1 | 1.0 | 0.3847 | inf | 754.25 |
| 7 | 5 | 0.0 | -0.8784 | 0.0031 | -11361.87 |
| 8 | 4 | 0.25 | -0.1025 | 0.7614 | -345.93 |

### Walk-forward windows (half-years)

| window | trades | win_rate | expectancy_r | profit_factor | net_usd |
| --- | --- | --- | --- | --- | --- |
| 2022H2 | 10 | 0.1 | -0.5989 | 0.075 | -15910.32 |
| 2023H1 | 6 | 0.3333 | -0.4473 | 0.2569 | -5237.94 |

### Monte Carlo (bootstrap of realised R, 30 trades, risk 3.2%)
P(max DD > 15%) 100%, P(max DD > 20%) 99%, median max DD 35.3%, P(below start) 100%

## Holdout (2026-05-03 to 2026-11-03)
No trades.

## Decision funnel
| outcome | count |
| --- | --- |
| APPROVED | 16 |
| EXECUTED | 16 |
| PORTFOLIO:PM_SYMBOL_ALREADY_OPEN | 39 |
| PORTFOLIO:PM_CORRELATION_BLOCK | 13 |
| ADVERSARIAL:ADV_RSI_EXHAUSTION_BUY | 1019 |
| HARD_RISK:RISK_DAILY_STOP | 11 |
| HARD_RISK:RISK_WEEKLY_STOP | 200 |
| ADVERSARIAL:ADV_RSI_EXHAUSTION_SELL | 911 |
| PORTFOLIO:PM_EXPOSURE_LIMIT | 11 |
| HARD_RISK:RISK_DRAWDOWN_STOP | 43974 |
| ADVERSARIAL:ADV_SPREAD_TOO_WIDE | 1 |

## Candidate funnel (stage 1)

- EURUSD: SESSION_INACTIVE=40140, REGIME:REGIME_TRANSITION=22139, CANDIDATE=8232, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=7909, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5571, REGIME:REGIME_NO_TRADE=5419
- GBPUSD: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=23156, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=8306, CANDIDATE=6881, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5166, REGIME:REGIME_NO_TRADE=4747
- USDJPY: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=24346, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=7448, CANDIDATE=6517, REGIME:REGIME_NO_TRADE=5682, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=4974
- AUDUSD: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=22419, REGIME:REGIME_NO_TRADE=8503, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=8352, CANDIDATE=8294, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=4819
- USDCAD: SESSION_INACTIVE=40141, REGIME:REGIME_TRANSITION=21790, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=8666, CANDIDATE=7993, REGIME:REGIME_NO_TRADE=6184, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5115
- USDCHF: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=22566, CANDIDATE=8278, REGIME:REGIME_NO_TRADE=7375, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=6746, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5234
