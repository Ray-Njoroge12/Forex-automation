# Growth of a $10 live account under the current strategy (2026-11-04)

Question: starting a live MT5 account with $10, how long until it gains $10 (reaches $20), and how much would it hold after 24 hours?

**Short answer.** With the current strategy it does not reach $20: in 845 simulated trading days it never came close, and after 24 hours it holds about $9.4 to $9.6 on average. A standard account cannot run the system at $10 at all. Only a cent account can, and the engine has no live mode for it.

Evidence labels: **Modelled** (my simulation of the repo's real replay trades, scripts and outputs saved in `fx_ai_engine/artifacts/research/small-account-sim/`), **Verified** (read in the code today), **Source** (web, linked), **Not verified**.

---

## 1. What can run at $10

### 1.1 Standard account (0.01 lot = 1,000 units, about $0.10 per pip on EURUSD)

| Check | Result | Label |
| --- | --- | --- |
| Risk of one 0.01 lot at the replay's median stop (8.1 pips) | $0.74, or 7.4% of $10. Quartiles $0.51 to $1.07 | Modelled |
| Pre-route lot check (`evaluate_trade_feasibility`) at the locked 3.2% risk | Rejects any trade whose risk-sized lot is below 0.01 (`REJECTED_LOT_PREROUTE`). At $10 only **7.4%** of the strategy's signals pass | Verified + Modelled |
| Share of signals that pass by balance | $20: 40%, $25: 55%, $50: 92%, $100: 99.4%, $250 and up: 100% | Modelled |
| Margin for one 0.01 lot EURUSD (1,000 units at 1.125) | 1:100 leverage $11.25 (more than the balance), 1:200 $5.62, 1:400 $2.81, 1:500 $2.25 | Modelled |
| Preserve-$10 startup gate (`main.py`) | Rejects with `PRESERVE_10_ECONOMICS_TOO_COARSE` when a 0.01 lot costs more than $0.01 per pip, which is $0.10 on a standard account | Verified |

A standard account needs roughly $50 or more before the system can size most of its trades.

### 1.2 Cent account (balance in cents; far finer lot granularity)

The repo's own $10 doctrine (`preserve_10`: fixed $0.50 risk, one trade, daily stop 15%, weekly 25%, drawdown halt 30%, 2-loss halt) passes its startup gate only on a cent account, using the `USC` denomination map (`core/mt5_bridge.py`).

Cent accounts with $10 to $15 minimum deposits exist: TMGM from $15 on MT5 with up to 1:500 leverage ([TMGM](https://www.tmgm.com/en/trading/cent-account)), LiteFinance from $10 ([LiteFinance](https://www.litefinance.org/trading/account-types/cent/)), and Exness operates a CMA-licensed Kenyan entity ([Exness Kenya](https://www.exness.ke/standard-accounts/)); whether it offers a cent account is **not verified**. These are not recommendations. The replay costs ($7 per lot round trip commission, 0.2 pip slippage) assume raw-spread pricing. Cent accounts normally run wider Standard-type spreads, so real results would be worse. **Not verified.**

### 1.3 What blocks live trading regardless of account type

- `main.py --mode` accepts only `smoke` and `demo`; demo mode refuses a non-demo account. There is no live mode.
- `preserve_10` is a non-SRS policy. Non-SRS policies are allowed only on a mock or demo account (`NON_SRS_POLICY_REQUIRES_MOCK_OR_DEMO`).
- The EA ignores signals on a real account (`RequireDemoAccount = true`).
- A loss-streak halt is cleared only by a winning trade, and no trades are accepted while halted. Live, it needs a manual reset. The replay assumes a weekly reset, and so does this model.

---

## 2. Method

Modelled with the strategy's own results, not a made-up return: 6,406 trades from the unified replay (2022-12 to 2026-11, six pairs, M15, net of costs, mean -0.348R). Each trade keeps its real entry and exit time, stop size and R multiple. Dollar profit is `risk in dollars x R`.

Each account type applies its own rules: lot rounding and the pre-route check (standard account), risk per trade (3.2% of equity, or $0.50 fixed), the loss throttle, daily, weekly and drawdown stops, the loss-streak halt with a weekly reset, and one trade open at a time. Trades overlapping an open trade are skipped.

Three set-ups:

| Set-up | Rules |
| --- | --- |
| **A** Standard account, core SRS | 3.2% risk, daily stop 8%, weekly 15%, drawdown halt 20%, 3-loss halt, throttle 0.75 and 0.5 |
| **B** Cent account, core SRS | Same rules, no lot-granularity limit |
| **C** Cent account, Preserve-$10 | $0.50 fixed risk (5% of $10), daily stop 15%, weekly 25%, drawdown halt 30%, 2-loss halt, throttle 0.75 |

"24 hours" is one UTC trading day started fresh from $10. All 845 weekdays in the history are used, so there is no sampling noise. Time to $20 uses 3,000 paths of up to 250 trading days built by drawing whole historical weeks at random.

---

## 3. After 24 hours (845 historical weekdays, fresh $10 each)

| Set-up | Mean | Median | 5th to 95th percentile | Ends above $10 | Ends below $10 | Trades per day | Best day | Worst day |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A Standard, core SRS | $9.95 | $10.00 | $9.61 to $10.00 | 3.1% | 16.7% (80% of days: no trade) | 0.25 | $11.23 | $9.12 |
| B Cent, core SRS | $9.57 | $9.41 | $9.03 to $10.65 | 18.3% | 81.7% | 4.3 | $12.86 | $8.87 |
| C Cent, Preserve-$10 | $9.44 | $9.23 | $8.58 to $10.93 | 17.5% | 82.5% | 3.2 | $14.25 | $8.12 |

- Reaching $20 in a day: **0 of 845 days** in every set-up. The best single day was +$4.25 (set-up C).
- Expected result of a day: **-$0.43 (B) and -$0.56 (C)**, about 4% to 6% of the account. That matches -0.35R per trade times 3 to 4 trades times $0.32 to $0.50 risk.
- In B, 32% of days reach the 8% daily stop. In C the 15% daily stop is reached on 3.7% of days.

---

## 4. Time to reach $20

Week-block bootstrap, 250 trading days (about 50 weeks), 3,000 paths each.

| Set-up | Reaches $20 within 30 / 90 / 250 days | Hit the drawdown halt | Median ending balance | Ended above $10 |
| --- | --- | --- | --- | --- |
| A Standard, core SRS | 0% / 0% / 0% | 0.5% (it rarely trades) | $9.38 | 8.1% |
| B Cent, core SRS | 0% / 0% / 0% | 82.8% | $7.99 | 3.3% |
| C Cent, Preserve-$10 | 0% / 0% / 0% | 66.9% | $7.09 | 4.6% |

With this strategy the expected time to +$10 is **never**. In two thirds to four fifths of paths the engine stops itself at the drawdown halt first (in B at 20%, about $8; in C at 30%, about $7).

---

## 5. What an edge would change (hypothetical, not the current strategy)

The same trades with every R shifted by a constant. This shows how far a strategy that works would be from the goal.

| Expectancy per trade | Set-up | Mean after 24 h | Ends a day above $10 | Reaches $20 by 30 / 90 / 250 days | Median day when reached | Drawdown-halted |
| --- | --- | --- | --- | --- | --- | --- |
| about 0.00R | B | $9.99 | 39% | 0.4% / 0.9% / 1.4% | 50 | 27% |
| about 0.00R | C | $9.96 | 36% | 0.1% / 0.4% / 0.6% | 52 | 14% |
| about +0.30R | B | $10.43 | 59% | 5.6% / 12.7% / 19.8% | 65 | 0.1% |
| about +0.30R | C | $10.45 | 50% | 2.6% / 4.0% / 5.3% | 32 | 0.1% |
| about +0.60R | B | $10.88 | 73% | 17.9% / 30.6% / 41.5% | 40 | 0% |
| about +0.60R | C | $10.94 | 62% | 5.6% / 10.0% / 14.7% | 54 | 0% |

Back-of-envelope with fixed $0.50 risk and 3.2 trades a day: gaining $10 takes `20 / E` trades, where E is the expectancy in R. At +0.1R that is 200 trades (about 62 days), at +0.3R 67 trades (about 21 days), at +0.5R 40 trades (about 12 days). This ignores variance and halts, so it is a lower bound; the simulated medians above are longer. Doubling a $10 account in 24 hours would need an expectancy above +6R per trade, which is not realistic.

---

## 6. Conclusions

1. **Current strategy:** expect to lose about $0.45 to $0.55 in a day and to be stopped out by the drawdown halt within weeks in most paths. It never reaches $20 in the modelled history.
2. **Standard account:** cannot run the system at $10. The pre-route check rejects 93% of signals, and 1:100 leverage cannot margin even one 0.01 lot.
3. **Cent account:** feasible for sizing, but the engine has no live mode, and the `preserve_10` policy is demo-only by design. A live cent account needs the same guarded live build and SRS v1.1 gate described in `2026-11-04-mt5-demo-and-live-account-assessment.md`.
4. **Even a working strategy is slow at $10.** At +0.3R the median time to double is 32 to 65 trading days, and at most about a fifth of paths get there within a year.
5. The repo's `REQUIREMENTS_CAPITAL_GROWTH_PROGRAM.md` frames $10 as the starting capital. Its own caveat holds: demo results cannot prove micro-capital viability, and the shadow-equity track (Track B) must pass first. Today it would not.

Limits of the model: replay costs are raw-spread pricing; trades are filled at the replay's price with no extra slippage; overlapping trades are skipped, which may differ from the live portfolio manager; the weekly loss-streak reset is an assumption; a margin call and broker stop-out are not modelled; and 2022 to 2026 is one history. Results are indicative, not forecasts.

Reproduce: `../.venv/bin/python artifacts/research/small-account-sim/sim10_small_account_growth.py` from `fx_ai_engine/`. Output saved as `output_sim10.txt`.
