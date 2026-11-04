---
name: fx-drift-monitor
description: Run the R8 live-vs-replay drift monitor (research.drift_monitor) to place the realised MetaTrader 5 demo R sequence inside cumulative-R bands simulated from the replay trades with the wave 1 block bootstrap, and apply the investigate thresholds frozen in live_vs_replay_thresholds.json. Report only.
---
# FX live-vs-replay drift monitor (R8)
## When to run
- Weekly during the demo burn-in (plan Phase 6), next to the fill reconciliation (`fx-fill-reconciliation`) and the trade postmortem (`fx-trade-postmortem`).
- Whenever the demo account has a run of losses and someone asks whether it is still consistent with what the replay predicted.
- Before quoting a replay result as the expectation for the demo account: if live trades already sit outside the bands, the replay is the thing to question first.
## Commands
Run from `fx_ai_engine/`. Build the synthetic fixture first (36 closed trades, 200 replay trades at a 33% win rate):
```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.synthetic_demo --out ../tmp/wave3-fixture --seed 3
USE_MT5_MOCK=1 ../.venv/bin/python -m research.drift_monitor --db ../tmp/wave3-fixture/trading_state.db --replay-trades-csv ../tmp/wave3-fixture/replay_trades.csv --exits-dir ../tmp/wave3-fixture/exits --deals ../tmp/wave3-fixture/deals.json --out artifacts/research/wave3-fixture/r8
```
On real demo data, use copies only: a read-only snapshot of the state DB (`research.demo_records.snapshot_db` opens the live DB with `mode=ro`), `cp` copies of `market/deals.json` and `exits/exit_*.json`, and a copy of the replay `*_trades.csv`:
```
USE_MT5_MOCK=1 ../.venv/bin/python -m research.drift_monitor --db ../tmp/wave3-real/trading_state_snapshot.db --replay-trades-csv ../tmp/wave3-real/baseline__policy__edge__base_trades.csv --exits-dir artifacts/research/wave3-real/inputs/exits --deals artifacts/research/wave3-real/inputs/deals.json --out artifacts/research/wave3-real/r8
```
`--deals` is required, because live R is computed from the deals. Options: `--gate-verdict <r2>/verdict.json` (the R2 authenticity gate result for that replay), `--evidence-stream` (default `runtime_mt5_core_srs`), `--thresholds` (default `research/live_vs_replay_thresholds.json`). Outputs `drift_monitor.json`, `drift_monitor.md` and `bands.csv` (one row per live trade).
## How to read the output
- Live sequence: closed strategy trades from the DB, plus open trades whose final exit file copy shows them closed, ordered by the final closing deal's time (a partial close is not the exit). Each trade enters at its net R: profit + commission + swap + fee over all its deals, divided by risk in USD (lots × stop pips × USD pip value). That is how the replay defines `r_multiple`. The EA's own `r_multiple` leaves out commission and swap, so it is not used. Trades with no closing deal are left out and counted in the report. EA demo-matrix test deals have no trades row and are left out.
- Bands: for each step k (the k-th live trade), the replay R values are resampled into many k-trade paths (block bootstrap, so loss clusters survive) and the table shows the 5th to 95th percentile of simulated cumulative R next to the live cumulative R.
- Rank: `100*P(sim <= live)`, the share of simulated paths at or below the live value. Near 50 is typical, near 0 means live is worse than almost every path.
- Final metrics: cumulative R after n trades, maximum drawdown in R (from a peak that includes the starting 0), and the longest run of losing trades, each with its simulated percentiles.
- Flags: `CUM_R_BELOW_BAND` (live cumulative R in the low tail), `DRAWDOWN_ABOVE_BAND` and `LOSS_STREAK_ABOVE_BAND` (live worse than the high tail), and `CUM_R_ABOVE_BAND` (live far better than the replay; check the recording before celebrating). Ties count against firing: below-flags use `P(sim <= live)`, above-flags `P(sim < live)`, because R values such as +2.2 and -1.0 land on a lattice where many paths tie.
- Verdict: `INCONCLUSIVE` while live trades are fewer than `min_live_trades` (flags then are informational), `INVESTIGATE` if any flag fired, else `WITHIN_BANDS`. `INVESTIGATE` means a human looks at the trades, the fills (R7) and the postmortem (R9). It is not a signal to stop or resize anything.
## FX/SRS specifics
- Report only: no halt or veto authority, and nothing in the engine reads this output. The SRS halts (daily, weekly, drawdown, consecutive losses) stay with the Hard Risk Engine.
- Thresholds, run count, block length and seed live in `research/live_vs_replay_thresholds.json` (`r8_drift_monitor`), written down before any real data was read. Every report names the file version and its sha256 prefix, so a reader can tell which rules produced it. Changing a threshold after seeing results would turn the monitor into a fit to the data; change one only by adding a new version with the reason in ADR 0004.
- The bands are in R, not in account percent, so they do not depend on the 3.2% risk per trade. For halt probabilities at a given risk level, use `fx-monte-carlo-risk-sweep`.
- Exit times come from `deals.json` (`time_msc`, UTC). Exit files carry server time (UTC+2 on MetaQuotes-Demo).
- Always `USE_MT5_MOCK=1`. Never `main.py --mode demo`, never an MT5 `trade_*` tool. The CLI refuses an `--out` inside any `MQL5/Files/bridge` folder.
## Pitfalls
- Without `--gate-verdict`, or with an R2 verdict of FAIL, the replay distribution is NOT EVIDENCE and the report says so. Bands built from an ungated replay can only describe that replay.
- R2's `verdict.json` does not record a hash of the replay trades CSV. The tool checks the trade count it does record: a different count makes the bands NOT EVIDENCE, and an equal count is shown as binding `UNVERIFIED`. Confirm by hand that the verdict came from the same replay run as the CSV.
- With the handful of real demo trades available, the verdict is `INCONCLUSIVE`. Report it that way; a single trade inside or outside a band says nothing.
- The replay and the demo must run the same policy and cost scenario. Comparing demo trades with a replay of another variant measures the variant difference, not drift.
- Block bootstrap needs the replay trades in time order; the tool sorts by `exit_time`. A CSV without that column cannot be used.
- A trade closed by hand (exit deal without the EA magic number or an `[sl]`/`[tp]` tag) still counts here at its net R. R9 flags it; whether such trades should be dropped from R8 is an owner decision (integration notes (e)).
## Inspiration
Ideas, not text, from [Kryhr/26-quant-trading-skills](https://github.com/Kryhr/26-quant-trading-skills) (`live-vs-backtest-drift-monitoring`).
