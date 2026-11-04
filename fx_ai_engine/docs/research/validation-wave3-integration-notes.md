# Validation wave 3: integration notes

Branch `research/validation-wave3` adds R7 (`research.fill_reconciliation`), R8 (`research.drift_monitor`) and R9 (`research.trade_postmortem`) without editing any existing file. These are notes for the owners of the reporting, EA and backtesting code. Nothing below has been applied.

(a) **A2 daily/weekly reporting.** The A2 report (plan Phase 6, daily "spread and slippage at fill" and weekly review) could call the three CLIs on a read-only snapshot of the state DB and on copies of the bridge files, never on the live folder:

```sh
# from fx_ai_engine/, snapshot and copies prepared beforehand
USE_MT5_MOCK=1 ../.venv/bin/python -m research.fill_reconciliation --db <snapshot.db> --deals <deals copy> --bars-dir <bars copy dir> --out <dir>/r7
USE_MT5_MOCK=1 ../.venv/bin/python -m research.drift_monitor --db <snapshot.db> --replay-trades-csv <gated replay trades> --exits-dir <exits copy> --deals <deals copy> --gate-verdict <verdict.json> --out <dir>/r8
USE_MT5_MOCK=1 ../.venv/bin/python -m research.trade_postmortem --db <snapshot.db> --deals <deals copy> --bars-dir <bars copy dir> --exits-dir <exits copy> --week <YYYY-Www> --out <dir>/r9
```

The outputs are report-only. Nothing in the engine should read them, and none of their verdicts may halt, veto or size a trade.

(b) **EA exit `close_time` time base.** The deployed EA (build 2026.11.03-2) wrote `exits/exit_59159851261.json` with `close_time` `2026-11-03T21:03:16Z`, while the matching deal in `market/deals.json` is at 19:03:16.520 UTC, an offset of 7,200 s (the server offset). Commit 4d4d329 describes a fix for this labelling, so check which EA build is actually deployed. Until then, wave 3 takes exit times from `deals.json` and records the offset per trade.

(c) **Canonical replay entry bar.** `unified_replay` fills a candidate decided at M15 bar close T at the open of the bar starting at T. `replay_execution.market_entry` fills only on a bar starting strictly after the submission time, which is the next bar when the decision is a few seconds after T. On the one real GBPUSD deal the two references differ by 2.0 pip (fill diff −1.1 pip against unified_replay, +0.9 pip against the strict reference). R7 reports both. The backtesting owner should decide which one is canonical.

(d) **Spread floors vs MetaQuotes-Demo.** The base scenario's GBPUSD spread floor is 1.2 pip. MetaQuotes-Demo reported 0.1 pip at the real fill, and demo commission was 0, so the demo looks cheaper than the model (cost ratio 0.143). The R7 suggestion stays data only until at least 20 comparable deals exist and a demo on the broker you intend to fund is running (plan D1). The demo's spreads and commissions do not represent a funded account.

(e) **Possible manual close.** R9 classified the live GBPUSD trade AI_20261103_163004_0564f3 as EXECUTION because its exit deal (19:03:16 UTC, price 1.34242) had magic 0 and no `[sl ...]`/`[tp ...]` tag, which means neither the EA stop nor the EA target closed it. Confirm whether it was closed by hand or by another process. If it was a manual close, its +0.4926 R should not count as a strategy result.

Fixture and real-data results are in `fx_ai_engine/artifacts/research/wave3-fixture/` and `wave3-real/` and are summarised in `docs/decisions/0004-validation-wave3-live-vs-replay.md`.
