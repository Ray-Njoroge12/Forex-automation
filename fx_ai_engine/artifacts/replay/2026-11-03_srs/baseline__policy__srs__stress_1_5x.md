# Replay report: baseline__policy__srs / stress_1_5x

Generated 20261103T174558Z. Policy hash `f00f1f9c3e91`. Datasets: AUDUSD_D1.csv, AUDUSD_H1.csv, AUDUSD_H4.csv, AUDUSD_M15.csv, EURUSD_D1.csv, EURUSD_H1.csv, EURUSD_H4.csv, EURUSD_M15.csv, GBPUSD_D1.csv, GBPUSD_H1.csv, GBPUSD_H4.csv, GBPUSD_M15.csv, USDCAD_D1.csv, USDCAD_H1.csv, USDCAD_H4.csv, USDCAD_M15.csv, USDCHF_D1.csv, USDCHF_H1.csv, USDCHF_H4.csv, USDCHF_M15.csv, USDJPY_D1.csv, USDJPY_H1.csv, USDJPY_H4.csv, USDJPY_M15.csv.

Assumptions: completed bars only; next-M15-open market fills; BID bars with ask = bid + max(recorded, floor) spread x cost multiplier; slippage 0.2 pips and $7/lot round-trip commission x multiplier; stop assumed before target within a bar; EA break-even/partial/trail applied from bar extremes, effective next bar; no calendar blackout, swap, or partial fills; loss-streak halt cleared at the next ISO week (operator review); candidates generated once with base costs.

## Development (2022-12-24 to 2026-05-03)
- Trades 17, win rate 29.4% (SRS r>0: 29.4%), break-even 5.9%
- Expectancy -0.539R (95% CI -0.860 to -0.177), profit factor 0.1659, avg winner 0.53R, avg loser -1.07R
- Return -21.2%, CAGR -6.9%, max drawdown 21.6%, longest losing run 2
- Exits: {'STOP': 17}

### By symbol
| symbol | trades | win_rate | expectancy_r | profit_factor | net_usd |
| --- | --- | --- | --- | --- | --- |
| AUDUSD | 5 | 0.6 | -0.0591 | 0.7417 | -1155.66 |
| EURUSD | 4 | 0.0 | -0.8999 | 0.0 | -8331.72 |
| GBPUSD | 2 | 0.0 | -1.0787 | 0.0 | -5034.83 |
| USDCAD | 2 | 0.5 | -0.295 | 0.2289 | -1314.32 |
| USDJPY | 4 | 0.25 | -0.6286 | 0.087 | -5381.3 |

### By entry hour (UTC)
| hour | trades | win_rate | expectancy_r | profit_factor | net_usd |
| --- | --- | --- | --- | --- | --- |
| 10 | 2 | 0.5 | -0.2321 | 0.3956 | -1260.57 |
| 13 | 1 | 0.0 | -1.082 | 0.0 | -2949.21 |
| 15 | 2 | 0.0 | -0.8483 | 0.0 | -3346.89 |
| 16 | 2 | 1.0 | 0.3695 | inf | 902.96 |
| 20 | 1 | 0.0 | -1.0934 | 0.0 | -1704.46 |
| 7 | 6 | 0.1667 | -0.6433 | 0.1478 | -9162.97 |
| 8 | 2 | 0.5 | -0.2975 | 0.5519 | -734.11 |
| 9 | 1 | 0.0 | -1.1047 | 0.0 | -2962.57 |

### Walk-forward windows (half-years)

| window | trades | win_rate | expectancy_r | profit_factor | net_usd |
| --- | --- | --- | --- | --- | --- |
| 2022H2 | 3 | 0.0 | -0.8189 | 0.0 | -5846.64 |
| 2023H1 | 14 | 0.3571 | -0.4786 | 0.2155 | -15371.19 |

### Monte Carlo (bootstrap of realised R, 30 trades, risk 3.2%)
P(max DD > 15%) 100%, P(max DD > 20%) 99%, median max DD 36.2%, P(below start) 100%

## Holdout (2026-05-03 to 2026-11-03)
No trades.

## Decision funnel
| outcome | count |
| --- | --- |
| APPROVED | 17 |
| EXECUTED | 17 |
| PORTFOLIO:PM_SYMBOL_ALREADY_OPEN | 43 |
| HARD_RISK:RISK_LOSS_STREAK | 241 |
| ADVERSARIAL:ADV_RSI_EXHAUSTION_BUY | 966 |
| ADVERSARIAL:ADV_RSI_EXHAUSTION_SELL | 840 |
| ADVERSARIAL:ADV_SPREAD_TOO_WIDE | 3088 |
| PORTFOLIO:PM_CORRELATION_BLOCK | 8 |
| PORTFOLIO:PM_EXPOSURE_LIMIT | 11 |
| HARD_RISK:RISK_DRAWDOWN_STOP | 40981 |

## Candidate funnel (stage 1)

- EURUSD: SESSION_INACTIVE=40140, REGIME:REGIME_TRANSITION=22139, CANDIDATE=8232, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=7909, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5571, REGIME:REGIME_NO_TRADE=5419
- GBPUSD: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=23156, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=8306, CANDIDATE=6881, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5166, REGIME:REGIME_NO_TRADE=4747
- USDJPY: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=24346, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=7448, CANDIDATE=6517, REGIME:REGIME_NO_TRADE=5682, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=4974
- AUDUSD: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=22419, REGIME:REGIME_NO_TRADE=8503, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=8352, CANDIDATE=8294, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=4819
- USDCAD: SESSION_INACTIVE=40141, REGIME:REGIME_TRANSITION=21790, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=8666, CANDIDATE=7993, REGIME:REGIME_NO_TRADE=6184, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5115
- USDCHF: SESSION_INACTIVE=40143, REGIME:REGIME_TRANSITION=22566, CANDIDATE=8278, REGIME:REGIME_NO_TRADE=7375, TECHNICAL:TECH_HIGHER_TIMEFRAME_MISALIGNED=6746, TECHNICAL:TECH_CANDLE_CONFIRMATION_FAILED=5234
