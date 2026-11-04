# Growing a $10 live MT5 account with this system: integration, progress and pathway (2026-11-04)

Direction from the owner (2026-11-04): the $10 live account is the target, and the analysis should show how the system can grow it trade by trade. This document records how MT5 is integrated today, how far the system has come, what a $10 account demands of it, and the steps that stand between now and a live $10 trade. It replaces the cautionary tone of the earlier $10 notes with a requirements view: what has to be true for the account to grow.

Evidence labels: **Verified** (code or record read on 2026-11-04), **Modelled** (my simulation; scripts and outputs in `fx_ai_engine/artifacts/research/small-account-sim/`), **Design** (the Stage D design on `dev/development-wave`, uncommitted, not yet replayed), **Not verified**.

---

## 1. Bottom line

1. **The machinery is ready. The edge is not proven.** Plumbing, risk controls, replay and validation tooling now exist and are tested. No strategy has yet shown a positive expectancy after costs; the only strategy that has been replayed loses 0.35R per trade.
2. **The pending strategy work targets the right problem.** Costs take about 0.31R per trade on 8-pip stops. The Stage D hypotheses use 20 to 60 pip stops, which cut cost to roughly 0.04 to 0.12R. That lowers the win rate needed to break even from 41% to about 33% to 35%. It does not by itself create an edge.
3. **At $10 growth is cents per trade and months, not hours.** At 1.5% risk a $0.15 stake earns about $0.04 per trade at +0.25R expectancy. Doubling takes about 185 trades.
4. **Risk per trade should be about 1.5%, not 3.2%.** With a real edge, 3.2% risk still ends in the 20% drawdown halt in about 95% of paths. At 1.5% it is about 11%, and 88% of paths reach $20 within a year.
5. **Only a cent account fits.** Wider stops make a standard account worse at small balances.
6. **Five things must happen before a live $10 trade:** a strategy that passes the replay gate, a cent-broker demo that confirms its contract and spreads, a guarded live mode, the lower micro risk policy, and your written approval.

---

## 2. How MT5 is integrated (Verified)

### 2.1 The loop, in order

| Step | Who | What happens | Timing |
| --- | --- | --- | --- |
| 1 | EA (`OnTimer`, 1 s) | Exports closed bars for 6 pairs x M15/H1/H4/D1 (400 bars each) when a bar closes, quotes and account snapshot every 5 s, symbol specs, 30-day deal history, calendar | continuous |
| 2 | Python engine | Polls every 5 s; on a new M15 candle runs the decision cycle per symbol: Strategy (Regime, Technical) then Adversarial, Portfolio Manager, Hard Risk Engine, reservation | up to about 6 s after a bar closes |
| 3 | Signal router | Writes a signal envelope v2 atomically (`tmp` then rename) into `pending_signals/` | milliseconds |
| 4 | EA `ProcessPendingSignal` | Checks, in order: demo account required, submission marker (restart safety), already processed, schema version 2, policy hash pin, expiry, symbol selected, tick available, spread against `MaxSpreadPips` (2.0) and the signal's cap, open positions plus orders against the signal's cap | within 1 s |
| 5 | EA sizing | **Recomputes the lot itself** from live balance, the signal's `risk_percent` and `stop_pips`; the Python lot is only a cap (the smaller wins). Below the broker minimum it returns `REJECTED_LOT` | at send time |
| 6 | EA preflight | Trade mode, stop and freeze levels, then `OrderCheck` with the symbol's supported filling mode (FOK, then IOC, then RETURN), magic number and 10-point deviation | before send |
| 7 | EA send | Writes `submission_<id>.lock` first, then sends a market order with SL and (in fixed mode) TP; any doubtful outcome becomes `EXECUTION_UNCERTAIN` and keeps risk reserved | one request |
| 8 | EA management (each second) | Break-even, 50% partial close, ATR trail, per signal fields; state persisted per ticket so a restart resumes it | continuous |
| 9 | EA close handling | On the final exit deal, writes `execution_<id>.json` / `exit_<ticket>.json` | on close |
| 10 | Python feedback | Applies feedback, reservation release, lifecycle event and dedupe receipt in one DB transaction; conflicts halt new entries | next poll |

Only the EA places orders (invariant 1). The Hard Risk Engine runs after every other component (invariant 2).

### 2.2 What this means for a $10 account

| Fact | Consequence |
| --- | --- |
| The EA sizes from the live balance, so a growing balance compounds automatically with no Python change | Percent risk compounds per trade |
| The lot calculation uses ratios of the account currency's tick value, so it works on a cent account in cents as well | Sizing is correct on a cent account (the Python startup gate also converts with a cent unit scale) |
| Lots round down to the broker's step, and a risk below one minimum lot is rejected | Each cent-lot step is a fixed slice of risk; granularity depends on the broker's cent contract |
| The EA rejects any signal when spread exceeds `min(MaxSpreadPips = 2.0, signal cap)` | Cent accounts often run wider spreads; many signals could be rejected unless the cap is reviewed (a protected EA input) |
| Fixed brackets (stage D strategies set break-even, partial and trail to 0) skip all management | With wide stops the position just waits for SL or TP. Partial close cannot work at 0.01 lot anyway |
| `PositionsTotal() + OrdersTotal()` counts every position on the account | A manual trade on the same account counts against the engine's limit |
| `RequireDemoAccount = true`; `main.py --mode` is `smoke` or `demo` only | No live path exists until a guarded live mode is built |
| A loss-streak halt clears only on a winning trade, and no trade is accepted while halted | Needs an operator reset step; the replay assumes weekly |

---

## 3. How the system has grown

| Measure | 2026-11-02/03 | 2026-11-04 |
| --- | --- | --- |
| Tests | 321 | 581 |
| CI | none | GitHub Actions on Python 3.11 to 3.13, green |
| EA acceptance cases run on the demo account | 1 | 8 |
| Engine trade routed, filled and closed | 0 | 1 (exit not by the EA) |
| `main.py` / `database/db.py` | 2,395 / 1,582 lines | 1,116 / 78 lines, behind service modules and a repository interface |
| Replay | Backtrader smoke test | Unified replay reusing live agents, bid/ask fills, walk-forward, 6,406 trades over 2022 to 2026 |
| Validation tooling | none | 15 research modules, 10 agent skills: lookahead audit, authenticity gate, cost stress, Monte Carlo with SRS halts, multiple-testing check, fill reconciliation, drift monitor, postmortem, freeze and go/no-go |
| Decisions on record | 1 ADR | 7 ADRs |
| Commits | about 40 | 87 (47 since 2026-11-02) |
| In progress (uncommitted) | none | Stage D: strategy plug-in interface and three new strategies, each with a pre-registered card and rejection rule |
| Strategy edge | none shown | **still none**: -0.35R per trade |

What changed in kind: the system can now say, with a reproducible and gate-labelled test, whether a strategy has an edge. What has not changed: it has not found one yet.

---

## 4. The per-trade economics at $10 (Modelled)

### 4.1 One trade

Expected dollars per trade = risk in dollars x expectancy E (in R).

| Risk per trade | Stake on $10 | E = +0.10R | E = +0.25R | E = +0.50R |
| --- | --- | --- | --- | --- |
| 1.5% | $0.15 | +$0.015 | +$0.037 | +$0.075 |
| 3.2% (locked SRS) | $0.32 | +$0.032 | +$0.080 | +$0.160 |
| 5.0% (Preserve-$10) | $0.50 | +$0.050 | +$0.125 | +$0.250 |

Trades to double with compounding (`ln 2 / ln(1 + f x E)`):

| Risk | E = +0.10R | E = +0.25R | E = +0.50R |
| --- | --- | --- | --- |
| 1.5% | 462 | 185 | 93 |
| 3.2% | 217 | 87 | 44 |

### 4.2 What the strategy must deliver

Round-trip cost is about 2.5 pips (spread floor, slippage, $7 per lot commission). In R it equals 2.5 divided by the stop in pips. With the locked minimum R:R of 2.2, break-even win rate is `(1 + c) / 3.2`, and the win rate for +0.25R is `(1.25 + c) / 3.2`:

| Stop | Cost per trade | Break-even win rate | Win rate for +0.25R |
| --- | --- | --- | --- |
| 8 pips (today) | 0.31R | 41.0% | 48.8% |
| 12 pips | 0.21R | 37.8% | 45.6% |
| 20 pips | 0.13R | 35.2% | 43.0% |
| 25 pips | 0.10R | 34.4% | 42.2% |
| 40 pips | 0.06R | 33.2% | 41.0% |
| 60 pips | 0.04R | 32.6% | 40.4% |

For reference, the current entries won 27.9% of trades at zero cost against a 29.3% break-even (replay, fixed 2.2R), and 22.5% after costs. The new strategies must lift the win rate by 12 to 15 points to reach +0.25R. Whether a London-open or two-day-channel breakout can do that is exactly what the Stage D replays decide. Typical breakout systems with a 2.2R target win well under half of their trades; treat 42% as a demanding target, not a given. **Not verified until replayed.**

### 4.3 Growth paths by win rate and risk (cost 0.10R, core SRS halts, 20,000 paths, 250 trading days)

1.5 trades a day on average:

| Risk | Win rate (E) | Reach $20 by 60 / 120 / 250 days | Median day | Hit drawdown halt | Median ending balance |
| --- | --- | --- | --- | --- | --- |
| 1.5% | 35% (+0.02R) | 0% / 0.3% / 2.9% | 190 | 83% | $9.39 |
| 1.5% | 40% (+0.18R) | 0.3% / 6.8% / 38% | 172 | 42% | $15.76 |
| 1.5% | 45% (+0.34R) | 2.9% / 37% / 88% | 130 | 11% | $31.87 |
| 1.5% | 50% (+0.50R) | 15% / 80% / 99% | 88 | 2% | $65.20 |
| 3.2% | 40% (+0.18R) | 15% / 23% / 24% | 52 | 99.5% | $10.84 |
| 3.2% | 45% (+0.34R) | 40% / 53% / 53% | 45 | 95% | $16.77 |
| 3.2% | 50% (+0.50R) | 69% / 78% / 78% | 36 | 78% | $55.19 |

Reading it:
- **3.2% is faster but fragile.** A normal losing run trips the 20% drawdown halt, and the halt is terminal until reset. With a 45% to 50% win rate, 78% to 95% of paths end halted.
- **1.5% is slower and survives.** It matches the earlier plan default for D3 (1.0% to 1.5%).
- **Without a real edge there is no growth.** At a 35% win rate (+0.02R) the account mostly stagnates or halts, and at 40% (+0.18R) only 38% of paths reach $20 within a year.
- With 1.0 trades a day instead of 1.5, the 1.5% row at 45% takes 165 days to reach $20 (74% by day 250).

### 4.4 What the account can size

Standard account, EURUSD-like $0.10 per pip on a 0.01 lot: balance needed for one 0.01 lot to fit the risk budget.

| Stop | 0.01 lot risks | Balance needed at 3.2% | at 1.5% |
| --- | --- | --- | --- |
| 8 pips | $0.80 | $25 | $53 |
| 20 pips | $2.00 | $62 | $133 |
| 30 pips | $3.00 | $94 | $200 |
| 60 pips | $6.00 | $188 | $400 |

The wide stops that fix the cost problem push a standard account out of reach until about $60 to $400. **At $10 only a cent account works.** For a cent account the minimum lot step is far smaller, so a 25-pip stop at 1.5% risk ($0.15) is a few cent-lot steps; the exact granularity and spread depend on the broker's contract, which the EA already exports in `market/symbols.json` (`tick_value`, `volume_min`, `volume_step`) and must be read on the chosen broker's demo.

---

## 5. Pathway

Each stage has a gate. A failed gate sends the work back, not forward.

| # | Stage | Gate | Owner decision / protected change |
| --- | --- | --- | --- |
| 0 | **Strategy evidence.** Run the Stage D replays (H-006, H-007, H-009) in a testing wave, gated by R1 to R5 and swap-adjusted | At least one strategy "not rejected": net expectancy at least +0.25R at 1.5x costs with at least 100 trades, profit factor at least 1.3, average winner at least 1.5R, and a confirming holdout. Note H-009 is replay-only until an SRS revision | You start the testing wave |
| 1 | **Cent-broker demo.** Open a demo on a candidate cent account; read its contract, spread and filling; deploy EA `2026.11.04-1`; repeat the acceptance matrix; run R7 | Cent contract passes the Preserve startup gate; typical spreads fit the EA cap; plain symbol names; hedging | Pick the broker; review a change to `MaxSpreadPips` if needed (EA input) |
| 2 | **Micro risk policy.** A policy mode with 1.0% to 1.5% risk and halts scaled to micro size (replacing the demo-only Preserve-$10 for live) | Replay of the chosen strategy under that policy keeps the drawdown halt probability low (target below 15%) | Approve D3 and the new mode (`config_microcapital.py` and policy hash are protected) |
| 3 | **Guarded live mode.** `--mode live` needing an explicit confirmation, a lot ceiling, a trade-count cap and a kill switch; an EA build with `RequireDemoAccount = false` and a netting guard; the SRS v1.1 pre-live gate | Second-model review; tests for each guard; EA matrix repeated on the demo | Written approval for `main.py`, EA and SRS changes |
| 4 | **$10 pilot.** Live cent account, $10 deposit, no top-ups. Rules agreed in advance: stop at -30% ($7), review every 20 trades with R7 (costs), R8 (drift) and R9 (causes) | Live cost within the replay's 1.5x assumption; no unexplained reconciliation conflict | You approve each review |
| 5 | **Growth ladder.** Percent risk compounds on its own. Reconsider the account type at about $60 to $400 (standard account becomes feasible with 20 to 60 pip stops) and the risk level as the sample grows | Hold risk at 1.5% until 100 live trades support the replay | Each change of risk is a policy change |

Dependencies that can run in parallel now: stage 1 (free), the Stage D code (other session), and the micro policy design.

### What I would change in the system for $10

1. **Make the micro policy a first-class mode.** `preserve_10` is a demo-only research mode with 5% fixed risk, which the models above show is too aggressive. A live-capable micro mode at 1.0% to 1.5% belongs in the policy set and in the EA's policy hash pin.
2. **Allow two open trades at 1.5% risk.** Two positions at 1.5% total 3.0%, inside the 5% exposure cap, so concurrent trades become possible without raising per-trade risk. At 3.2% the cap allows only one. The replay should measure how many extra trades this adds before it is relied on.
3. **Review the EA spread cap per account type.** At `MaxSpreadPips = 2.0`, a cent account with 1.5 to 2.5 pip spreads would reject many signals.
4. **Give the loss-streak halt an operator reset** and a documented procedure, since it never clears on its own.
5. **Add a per-trade growth ledger** to the postmortem output: risk dollars, R, cost in R, balance after, and cumulative cost drag, so every trade's contribution to growing the $10 is visible (R7 and R9 already hold the inputs).

---

## 6. Open items

| Item | Status |
| --- | --- |
| Which stage D strategy, if any, has an edge | Not verified; replays not run |
| Cent contract (pip cost per 0.01 lot, minimum margin), spreads, filling, hedging at the chosen broker | Not verified |
| Whether the loss-streak halt behaves with a weekly reset in practice | Assumption (as in the replay) |
| CMA licensing and cent-account availability for the candidate broker | Not verified |
| Withdrawal and funding costs on a $10 account | Not verified |
| Real-money fill quality and slippage on a cent account | Not verified |
| Tax and legal position for a Kenyan resident | Not researched |

## 7. Reproduce

From `fx_ai_engine/`: `../.venv/bin/python artifacts/research/small-account-sim/growth_requirements.py` (output in `output_growth_requirements.txt`). Earlier simulations: `sim10_small_account_growth.py` and `prop_challenge_sim.py` in the same folder.
