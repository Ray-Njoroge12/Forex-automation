# System assessment and open-source benchmark

> **Update 2026-11-04.** The status figures in section 1 are outdated: CI now exists, 581 tests pass, the EA acceptance matrix has run (7 of 7 cases), one engine trade was routed and filled, and a unified replay has produced results. Current state: `2026-11-04-system-capability-and-readiness.md`. The benchmark (sections 5), the strategy weaknesses (section 4) and the replay-based conclusion that no edge exists remain valid. Funded accounts: `2026-11-04-funded-account-and-broker-options.md`.

Date: 2026-11-03. Scope: current working tree (including uncommitted changes), the local MT5 terminal data folder, and public GitHub projects. This is a read-only assessment. No source file was changed to produce it.

How to read the evidence labels:

- **Verified** means I ran a command or read the code/log on this machine today.
- **Documented** means a repo document claims it and I did not re-verify.
- **Judgment** means my scoring from public documentation. Peer scores are not measurements.

---

## 1. Where the system is

**Stage: engineered prototype, not yet an integrated system.** It has never completed one signal-to-fill-to-feedback cycle on a broker account.

| Check | Result | Label |
| --- | --- | --- |
| Python tests | 321 passed in 8.7 s (`USE_MT5_MOCK=1`) | Verified |
| EA compile | `FX_Execution.mq5` built with 0 errors, 0 warnings at 12:18 today | Verified |
| EA on terminal | Attached to MetaQuotes-Demo (hedging, $100,000), account snapshots written every few seconds | Verified |
| EA acceptance matrix | One case run: `DEMO_CHECK_INVALID_SCHEMA`, correctly rejected. No valid, expired, duplicate, spread, capacity or restart case in the log | Verified |
| Trades ever recorded | `trades`, `account_metrics`, `risk_events`, `decision_funnel_events` all have 0 rows. Terminal shows 0 positions, 0 orders | Verified |
| Historical backtest result | None. `tests/fixtures/ohlc_fixture.csv` has 4 lines. No dataset manifests exist | Verified |
| Python can reach MT5 | No. `import MetaTrader5` fails on this Mac (arm64, Python 3.14.7). The package is Windows-only | Verified |
| Market context gate | Closed. Calendar ends 2026-09-29 but needs coverage to 2026-11-10. Rates last updated 2026-08-31 (64 days) | Verified |
| Uncommitted work | 13 modified and several new files, including the feedback-integrity and EA hardening work. Last commit is 2026-09-15 | Verified |
| CI | None (`.github/workflows` does not exist) | Verified |

### Maturity ladder

| Stage | Meaning | Status |
| --- | --- | --- |
| 0 Concept | SRS, architecture | Done |
| 1 Prototype | Components plus unit tests | Done |
| 2 Integrated | Engine produces a signal, EA fills it, feedback reconciles, on a demo account | **Not reached** |
| 3 Validated | Reproducible out-of-sample and cost-stressed replay, then forward demo | Not started |
| 4 Small live | Real money at minimum size, execution quality measured | Not started |
| 5 Production | Months of stable live operation | Not started |

---

## 2. Why trade implementation keeps stalling

These are ordered by how directly they stop a trade from happening. The first three are hard blockers today.

1. **No data path from MT5 to Python on this machine.** The engine reads bars through `MetaTrader5.copy_rates_from_pos`, which needs the Windows-only package. [mt5-mac-bridge](https://pypi.org/project/mt5-mac-bridge/) and [siliconmetatrader5](https://github.com/bahadirumutiscimen/silicon-metatrader5) exist, but their own documentation says the Wine/rpyc route suits development and forward testing and not live capital. `core/market_quote_snapshot.py` started a file-based alternative, but `load_demo_quotes` is called only from tests. It is not wired into the engine, and there is no bar export.
2. **Fail-closed market-context gate.** With the calendar and rates stale, the engine correctly refuses new decisions. Nothing can trade until reviewed data is installed through `validation.update_market_context`.
3. **EA policy pin.** `RequirePolicyHashPin=true` and `ExpectedPolicyHash=""` by default, so the EA rejects every valid signal until the hash from `validation.print_policy_hash` is set on the chart inputs. I could not verify whether it is set.
4. **Execution contract is unproven against a broker.** The EA never calls `OrderCheck` or `OrderCalcMargin`, does not read `SYMBOL_FILLING_MODE`, does not set deviation or a magic number, and does not check stops/freeze levels before the initial order. It only checks stop level when modifying stops. The August audit flagged this and it is still open.
5. **The environment is a weak proxy for a real broker.** MetaQuotes-Demo is a generic server. Traders report that demo spreads, slippage and tick timing differ from live brokers ([MQL5 forum thread](https://www.mql5.com/en/forum/507677)). Bar boundaries also depend on server time, which matters for the H4 and D1 logic here. Treat today's setup as a plumbing test only.
6. **Python 3.14.7 venv.** The requirement files state a tested range of 3.11 to 3.13. The suite passes, but reproducibility is not established.

---

## 3. Strengths

| Area | Evidence | Label |
| --- | --- | --- |
| Fail-closed execution design | Versioned signal envelope (schema v2, expiry, policy hash, max spread, max positions); atomic reservation ledger; submission marker written before `OrderSend`; uncertain outcomes keep risk capacity reserved | Verified in code |
| Feedback integrity | Feedback, lifecycle event, reservation change and dedupe receipt in one DB transaction; invalid feedback quarantined; conflicts halt new entries | Documented, tests present |
| Independent risk authority | `HardRiskEngine` rejects on reconciliation divergence, halt flag, daily 8%, weekly 15%, drawdown 20%, loss-streak halt, concurrency and combined exposure. Graduated throttle (75% then 50%) | Verified in code |
| Explainability | Named reason codes at every stage, `decision_funnel_events`, risk-event audit trail | Verified in code |
| Controlled experiments | Non-SRS modes need explicit env flags and mock mode; experiments default off and are demo-gated | Verified in code |
| Closed-bar discipline | Bars start at position 1, so the forming bar is excluded | Verified in code |
| Operational tooling | Watchdog, OpenTelemetry/Prometheus hooks, Streamlit dashboard, readiness preflight, reconciliation inventory, EA demo matrix generator | Verified present |
| Prior audit items closed | Reservation ledger, EA spread default (now 2.0), calendar freshness gate, DST-aware sessions, `INSERT OR REPLACE` removed, loss-streak seeding moved to `__init__`, returns-based correlation | Verified |

---

## 4. Weaknesses

### 4.1 Strategy and parameters (P0 for any claim of edge)

1. **The active policy loosens the strategy beyond its coded defaults, with no recorded evidence.** `config_microcapital.py` sets `pullback_buffer_pips=20` (code default 2), RSI bounds 0 to 100 (code default 40 to 65 for buys, 35 to 60 for sells), ADX gates 15/20 (default 20/25) and `trend_distance_threshold=0.0002` (default 0.0005). A 20-pip "pullback" on M15 is satisfied by nearly any trend-aligned bar. The RSI filter is effectively off. In practice the entry reduces to H1+H4 EMA alignment plus an EMA(50) slope-and-close check. This is a high-frequency, low-selectivity trend follower. Whether it has an edge is unknown.
2. **The nominal 2.2 R:R is not what the EA trades.** In default trending regimes `tp_mode=TRAIL`, so the EA sends no take-profit. Exits come from break-even at 0.8R (+1 pip), a 50% partial at 1.2R, and an ATR trail. Realized R will cluster near zero and at stop-outs, not at 2.2R.
3. **Win-rate accounting is soft.** `validate_demo.py` counts any `r > 0` as a win, so a break-even exit at +1 pip counts as a win. A 45% win-rate gate can be met cosmetically.
4. **Indicator warm-up differs between live and replay.** Live fetches 350 bars for EMA(200). EMA is recursive, so the live value carries about 3% weight from the first bar, while a long replay carries none. [Freqtrade documents](https://www.freqtrade.io/en/stable/recursive-analysis/) this "recursive" bias and ships a command that measures how indicator values change with the startup window. No equivalent check exists here.
5. **Backtest resampling can include a partially formed final H1/H4 bar** (`bt_strategy._resample_backtest_ohlc`), which the 2026-11-02 roadmap already flags.

### 4.2 Risk and acceptance criteria (P0)

1. **SRS §12.2 is internally inconsistent.** It asks for win rate at least 45% and average R at least 2.0, and `validate_demo.py` computes average R over all trades. With a 2.2R target, mean R of 2.0 needs a win rate near 94%; the abort line (mean R below 1.8) needs near 88%. With any realistic win rate the demo would be rejected or aborted regardless of edge. The likely intent was average R of winners (the code already computes `avg_r_winners` as a diagnostic). This needs an SRS v1.x revision before the 30-day run.
2. **Per-trade risk of 3.2% is hard to reconcile with the drawdown gates.** My simplified simulation (2.2R wins, 1R losses, 0.08R cost per trade, throttle 75%/50%, independent trades, no break-even logic) gives:

   | True win rate | P(max DD > 15%), 30 trades | P(max DD > 20%), 30 trades | P(max DD > 15%), 100 trades |
   | --- | --- | --- | --- |
   | 40% | 55% | 28% | 96% |
   | 45% | 39% | 16% | 86% |
   | 50% | 24% | 7% | 67% |

   Even a genuine 50% win-rate edge breaches the SRS 15% drawdown acceptance limit about a quarter of the time over a 30-trade demo. This is a model, not a forecast, but it shows the risk parameter and acceptance gates do not fit together.
3. **A 25-trade sample cannot discriminate.** At a true win rate of 45%, there is a 45% chance of observing 45% or better over 25 trades, and a 24% chance of observing below 40% (which triggers the abort rule). At a true 35%, there is still a 13% chance of passing.
4. **"Max 2 trades" is effectively 1.** Two positions at 3.2% exceed the 5% combined cap. The second trade passes only if volatility-scaled risk falls to 1.8% or less, which needs ATR ratio of about 1.8 or higher (high-volatility conditions). This is a policy inconsistency rather than a bug.
5. **Correlation block uses absolute correlation.** `abs(corr)` discards sign, so two offsetting positions on opposite sides of correlated pairs are blocked as if they compounded risk.

### 4.3 Execution layer (P0/P1)

1. EA sizing (`CalculateLot`) is stop-distance only, uses `NormalizeDouble(lot, 2)`, and ignores spread, commission and slippage. Python feasibility mirrors it. Replay uses a different cost model.
2. Hand-written JSON parsing and ANSI file encoding in the EA.
3. Partial fills and pending (LIMIT) orders always map to `EXECUTION_UNCERTAIN`. That is safe but leaves an operator workflow to resolve them. The resolution process is documented as unfinished.
4. `main.py` is 2,286 lines and `database/db.py` is 1,508. The structural cleanup in the 2026-08-31 audit has not started.
5. Signals are published for market execution at the next poll after an M15 close. There is no measurement yet of the latency between bar close, signal write and fill.

### 4.4 Validation (P1)

1. The existing Backtrader path is labelled a smoke test in code (fixed 1.2-pip spread, zero slippage, zero commission, single symbol, no hard-risk engine).
2. `backtesting/replay_execution.py` has bid/ask fill primitives but is not connected to strategy or portfolio replay.
3. No dataset, no manifest, no walk-forward output, no cost-stress result.
4. No lookahead or recursive-bias check exists for the live-versus-replay indicator path.

---

## 5. Benchmark against open-source trading systems

### 5.1 What this comparison can and cannot say

- No public repository offers audited profitability evidence, so none is ranked on returns. The ranking compares engineering capability: execution integrity, risk controls, simulation realism, validation tooling, tests, deployability and maturity.
- The most-starred systems are general frameworks, and most do not support spot FX on MT5. [Freqtrade is crypto-only](https://autotradelab.com/blog/nautilus-vs-vectorbt-vs-freqtrade-20-python-quant-trading-frameworks-compared). The MT5-native niche is small and low-star.
- Scores are my judgment from READMEs and documentation. I read this repository's code, but I did not read the peers' code. Treat peer scores as approximate.

### 5.2 Repository facts (GitHub API, 2026-11-03)

| Project | Stars | Last push | License | Notes |
| --- | --- | --- | --- | --- |
| [freqtrade](https://github.com/freqtrade/freqtrade) | 56,366 | 2026-11-03 | GPL-3.0 | Crypto only; built-in lookahead and recursive analysis |
| [qlib](https://github.com/microsoft/qlib) | 49,955 | 2026-10-30 | MIT | ML research platform, not an execution system |
| [vnpy](https://github.com/vnpy/vnpy) | 46,213 | 2026-10-27 | MIT | Not assessed further |
| [nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | 31,185 | 2026-11-03 | LGPL-3.0 | Rust core, backtest/live parity, FX support |
| [backtrader](https://github.com/mementum/backtrader) | 23,646 | 2024-08-19 | GPL-3.0 | Dormant for over two years |
| [Lean](https://github.com/QuantConnect/Lean) | 22,361 | 2026-11-02 | Apache-2.0 | Multi-asset; FX via brokers such as OANDA and IBKR |
| [hummingbot](https://github.com/hummingbot/hummingbot) | 20,918 | 2026-10-20 | Apache-2.0 | Crypto market making |
| [jesse](https://github.com/jesse-ai/jesse) | 8,761 | 2026-10-31 | MIT | Crypto research and live |
| [EA31337](https://github.com/EA31337/EA31337) | 1,347 | 2026-04-07 | GPL-3.0 | MT4/MT5 EA; latest release is a 2024 pre-release |
| [aiomql](https://github.com/Ichinga-Samuel/aiomql) | 154 | 2026-07-26 | MIT | Async Python wrapper for MT5 |
| [pyeventbt](https://github.com/marticastany/pyeventbt) | 62 | 2026-10-14 | Apache-2.0 | Event-driven MT5 backtest and live with an MT5 API mock |
| [Algo-Trading-Engine](https://github.com/The-Vheed/Algo-Trading-Engine) | 6 | 2025-07-09 | MIT | MT5 via FastAPI bridge |
| **This project** | 0 | 2026-09-15 | none | 1 contributor, about 40 commits |

### 5.3 Scorecard (0 to 5 per dimension, judgment)

Dimensions: **A** execution and reconciliation, **B** risk controls, **C** simulation realism and live parity, **D** validation and bias tooling, **E** tests and engineering hygiene, **F** FX/broker coverage and works on this machine, **G** observability and operations, **H** maturity and community.

| Rank | System | A | B | C | D | E | F | G | H | Total /40 | Score /100 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | NautilusTrader | 5 | 4 | 5 | 3 | 5 | 4 | 4 | 5 | 35 | 88 |
| 2 | QuantConnect LEAN | 4 | 4 | 4 | 3 | 4 | 5 | 4 | 5 | 33 | 83 |
| 3 | Freqtrade (crypto only) | 3 | 3 | 3 | 5 | 5 | 1 | 4 | 5 | 29 | 73 |
| 4 | Jesse (crypto only) | 3 | 3 | 4 | 4 | 4 | 1 | 3 | 4 | 26 | 65 |
| 5 | Hummingbot (crypto only) | 4 | 3 | 2 | 2 | 4 | 1 | 4 | 5 | 25 | 63 |
| 6 | EA31337 | 3 | 3 | 3 | 2 | 3 | 4 | 2 | 3 | 23 | 58 |
| 7 | pyeventbt | 2 | 3 | 4 | 2 | 3 | 4 | 2 | 1 | 21 | 53 |
| **8** | **This project** | **3** | **4** | **1.5** | **2** | **3.5** | **1.5** | **3** | **0.5** | **19** | **48** |
| 9 | Backtrader | 3 | 2 | 3 | 1 | 3 | 2 | 1 | 3 | 18 | 45 |
| 9 | aiomql | 3 | 3 | 1 | 1 | 2 | 4 | 2 | 2 | 18 | 45 |
| 11 | Small MT5 hobby repos (for example Algo-Trading-Engine) | 1 to 2 | 2 to 3 | 1 to 3 | 1 | 1 to 2 | 3 to 4 | 1 | 0 to 1 | 10 to 14 | 25 to 35 |

Reasons for this project's scores:

- **A = 3.** The design would score 4 (reservation ledger, idempotent receipts, pre-send marker, uncertain-state handling). Nothing has run against a broker, so evidence caps it at 3. Nautilus sets the reference: its [Interactive Brokers adapter](https://nautilustrader.io/docs/latest/integrations/interactive_brokers/) reconciles from three independent broker sources (orders, executions, positions) and can adopt a position that has no matching order history.
- **B = 4.** Independent veto, layered stops, throttle, fail-closed state. The parameters and acceptance gates are the weak part (section 4.2).
- **C = 1.5.** Smoke-profile Backtrader and unconnected fill primitives. Nautilus's FX tutorial already includes a [probabilistic fill model and rollover interest](https://nautilustrader.io/docs/latest/tutorials/backtest_fx_bars/).
- **D = 2.** Manifests and an experiment guard exist, no results, no lookahead or recursive checks.
- **F = 1.5.** MT5 only, and the Python side cannot reach MT5 on this Mac today.
- **H = 0.5.** No users, one contributor.

### 5.4 Reading the ranking

- **Overall position:** about 8th of 11 in this field and roughly 40 points below the top. It sits just above dormant or thin MT5 wrappers and below every actively maintained framework.
- **Where it is genuinely ahead of its niche:** safety-first execution design (A design, B). `aiomql`, `pyeventbt` and the hobby repos do not document an equivalent reservation ledger, pre-send marker or policy-hash pinning. I did not audit their code.
- **Where it is far behind the leaders:** simulation realism (C), validation tooling (D), and the fact that none of it has run end to end (A evidence, F).
- **What the leaders have that this project lacks:** one code path for backtest and live (Nautilus), identical decision logic in simulation and production, built-in bias detection (Freqtrade), and reconciliation tested against venue reports (Nautilus).

---

## 6. Recommended path

The order matters. Each step unlocks the next.

### Step 1: reach Stage 2 on the demo terminal (days)

1. **Pick one MT5-to-Python data path.**
   - *Option A, file-based bars from the EA.* Extend the EA to write closed-bar OHLC for the six pairs and three timeframes, and add a file-backed `MT5Connection`. This matches the existing file bridge and avoids Wine/rpyc fragility. It also removes the need for the Python binding on macOS.
   - *Option B, [mt5-mac-bridge](https://pypi.org/project/mt5-mac-bridge/)* using the MetaTrader 5.app's own Wine prefix. It is closer to the current code, but it adds an unsupported emulation layer.
   - *Option C, a native Windows host* for anything beyond demo. Required before real capital in either case.
2. Install reviewed calendar and rate data with `validation.update_market_context`.
3. Pin `ExpectedPolicyHash` on the EA, then run the whole `ea_demo_matrix` (valid, expired, duplicate, over-spread, over-capacity, malformed, restart) and keep the logs.
4. Complete one valid trade end to end and reconcile the DB, the feedback files and the MT5 history.
5. Commit and push the current work. Thirteen modified files and the new modules exist only on this machine.

### Step 2: make the numbers meaningful (1 to 2 weeks)

1. Export the broker's M15 bars (the terminal already holds history for several pairs) and create manifests with `validation.prepare_replay_dataset`.
2. Fix the replay path: completed bars only, one shared decision and risk path, bid/ask fills, spread and slippage, stop gaps. Connect `replay_execution.py` to it.
3. Add a lookahead and recursive-warm-up check modeled on [Freqtrade's](https://www.freqtrade.io/en/stable/lookahead-analysis/): compare signals computed on 350 bars against signals on a long history.
4. Run ablations of the loosened parameters (20-pip buffer, RSI bypass, ADX 15/20) against the coded defaults on a held-out period. Record the result before changing anything.

### Step 3: repair the gates before the 30-day run (SRS v1.x)

1. Redefine §12.2 average R (winners only, or expectancy above a stated positive threshold, plus a minimum profit factor).
2. Reconcile risk per trade with the drawdown gates, or state explicitly that a breach of the 15% line has roughly a one-in-four chance even with a real edge.
3. Raise the trade-count minimum, or treat 25 trades as a plumbing test only.
4. Settle the 2-trade versus 5%-exposure contradiction.

### Step 4: structure and hygiene (parallel)

Add a CI workflow (clean venv on 3.11 to 3.13, non-MT5 tests, EA compile where feasible), split `main.py` and `db.py` as the August audit specified, and make the EA call `OrderCheck`, read the filling mode, and set deviation and a magic number.

### Step 5: broker realism

After the plumbing passes on MetaQuotes-Demo, repeat the matrix on a real broker's demo account (ideally the one you will fund). Then run a minimum-size live test, measuring spread, slippage and latency, before any scaling. This follows the demo, small-live, longer-live sequence described in [this MQL5 guide](https://www.mql5.com/en/blogs/post/766451).

---

## 7. Open questions for the owner

1. Which broker and account type will be funded? That decides the data path, the cost model, and whether Windows hosting is needed.
2. Was the Windows setup (`.mcp.json` points to `/mnt/c/Users/rayng/...`) ever run end to end? If so, is there trade history worth importing?
3. Is average R in SRS §12.2 meant as the all-trade mean or the winners' mean?
4. Were the 20-pip buffer and RSI bypass chosen deliberately to generate more trades? If so, what result motivated them?

Content from web sources was summarized and paraphrased for licensing compliance. Source links are inline.
