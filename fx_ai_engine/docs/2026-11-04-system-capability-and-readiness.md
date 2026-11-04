# System capability and readiness (2026-11-04)

Supersedes the status figures in `2026-11-03-system-assessment-and-benchmark.md`. That document keeps its benchmark and its reasoning about the strategy; its "where the system is" table is out of date (it reports no CI, 321 tests and no trades).

Evidence labels: **Verified** means I ran it or read the record on 2026-11-04. **Documented** means a repo document says so and I did not re-run it. **Not verified** means no evidence exists yet.

Branch `hardening/2026-11` at `267fc41`, pushed. GitHub CI passed on that push (`ci`, Python 3.11, 3.12, 3.13).

---

## 1. Verdict

The system now works end to end on a demo account. One engine-generated signal was routed, filled and closed, and every rejection path of the EA was exercised. It does not yet have a strategy worth funding: in replay, every configuration tested loses about 0.35R per trade after costs.

| Question | Answer |
| --- | --- |
| Does the plumbing work (data, decision, risk, execution, feedback)? | Yes on MetaQuotes-Demo, with the gaps in section 5 |
| Is it safe to leave running? | Not unattended. The engine was stopped on 2026-11-03 at the owner's request |
| Does the strategy make money? | No evidence of that. Replay says it loses |
| Is the testing evidence trustworthy? | Replay results are gate-labelled, and one demo exit was not made by the EA (section 3.3) |
| Ready for a funded account? | No. See `2026-11-04-funded-account-and-broker-options.md` |

---

## 2. Capability by component

| Component | State | Evidence |
| --- | --- | --- |
| Test suite | 581 tests pass (`USE_MT5_MOCK=1`, Python 3.12) | Verified, `581 passed in 45.78s` on the merged tree |
| CI | GitHub Actions on every push, Python 3.11 to 3.13, mock MT5 | Verified, last run succeeded |
| EA to Python data path | EA exports closed bars (6 pairs, M15/H1/H4/D1, 400 bars), quotes, account snapshot, calendar, deal history to `MQL5/Files/bridge/market` and `feedback`. `FileBridgeConnection` reads them and fails closed on stale or malformed files | Verified (file bridge check 43/43 per STATUS; live files present) |
| Decision pipeline | Regime, Technical, Adversarial, Portfolio Manager, Hard Risk Engine, reservation, router, in that order | Verified (356 decision events on 2026-11-03; golden-record contract test) |
| Hard Risk Engine | Daily 8%, weekly 15%, drawdown 20%, 3-loss halt, exposure caps, throttle | Verified in code and tests. Only 1 live `PORTFOLIO_RESERVATION` block observed |
| Execution | EA only. Spread, policy-hash, expiry, schema, duplicate and position-limit checks; `OrderCheck`; filling mode from the symbol; magic number; submission marker before send | Verified on demo (section 3.1) |
| Reconciliation | One DB transaction per feedback; conflicts halt entries; uncertain sends keep risk reserved | Verified in tests; 0 conflicts and 0 uncertain reservations in the demo DB |
| Replay | One code path for live and replay decisions; bid/ask fills; spread and slippage model; walk-forward; cost stress to 3x; Monte Carlo with SRS halts | Verified (parity test reproduces the live 2026-11-03 16:30 decisions) |
| Validation tooling | R1 lookahead and warm-up audit, R2 authenticity gate, R3 multiple-testing, R4 cost stress, R5 Monte Carlo, R7 fill reconciliation, R8 drift, R9 postmortem, R10 freeze and go/no-go | Verified (tests; fixture and real-data runs) |
| Structure | `main.py` 2,395 to 1,116 lines; `db.py` 1,582 to 78 lines with repositories; contract tests unchanged by the move | Verified |
| AI components | Meta-labeler scaffolding exists; 0 shadow events logged; no LLM in the decision path | Verified (`ml_shadow_events` = 0) |
| Operations | Detached long-job runner; launchd agents written but uninstalled; no alerting; no dashboard panels for blocks or reservations | Verified |

---

## 3. Demo account record

Account: MetaQuotes-Demo `5058284077`, hedging, USD. Balance 100,033.30 before the GBPUSD trade and 101,609.81 after.

### 3.1 EA acceptance cases (`fx_ai_engine/artifacts/demo-evidence/2026-11-03/`)

| Case | Run | Observed outcome | Expected |
| --- | --- | --- | --- |
| valid | `run_160754` | `EXECUTED`; BE moved the stop; position closed with +33.30 | Match |
| valid (first attempt) | `run_144539_valid_ordercheck` | `REJECTED_ORDER_CHECK` (3.33 lots, `OrderCheck` failed) | A defect found; the later run passes |
| duplicate (restart) | `run_160754` | `HELD_FOR_RECONCILIATION` | Match |
| position limit | `run_160754` | `REJECTED_POSITION_LIMIT` | Match |
| expired | `run_143100_rejections` | `REJECTED_EXPIRED` | Match |
| policy hash mismatch | `run_143100_rejections` | `REJECTED_POLICY_HASH` | Match |
| spread limit | `run_143100_rejections` | `REJECTED_SPREAD` | Match |
| malformed schema | `run_143100_rejections` | `DELETED_NO_FEEDBACK` | Match |

I did not find the cause of the first `REJECTED_ORDER_CHECK` in the evidence files; the result file records only the outcome.

### 3.2 Engine run, 2026-11-03 16:24 to 19:00 UTC (about 2.6 hours)

| Item | Value |
| --- | --- |
| Decision events | 356 |
| Trade proposals recorded | 23: 1 routed, 22 rejected |
| Top rejection reasons | `REGIME_NO_TRADE` 32, `REGIME_TRANSITION` 26, `TECH_CANDLE_CONFIRMATION_FAILED` 12, `PM_SYMBOL_ALREADY_OPEN` 11, `PM_EXPOSURE_LIMIT` 10, `TECH_RR_BELOW_MIN` 4, `NEWS_BLACKOUT` 2 |
| Routed trade | `AI_20261103_163004_0564f3`, GBPUSD BUY, 23.53 lots, entry 1.34175, stop 13.6 pips away, trailing mode with no take-profit, EA build `2026.11.03-2` |
| Exit | 1.34242 (+6.7 pips), +$1,576.51, 0.49R, at 19:03 UTC |
| Burn-in gate G1 | Not met: 1 of 5 trading days, engine stopped |

### 3.3 A finding that changes how the GBPUSD result reads

The exit deal of the GBPUSD position has `magic: 0` and an empty comment. The entry deal has the EA's magic number and the trade id as comment. The EA log shows `Finalized closed position` and no break-even, partial-close or trailing line for that ticket. The R9 postmortem flags the exit as "not placed by the EA: possible manual close".

Taken together, this is most likely a manual close from the terminal. I cannot prove it from the deal record alone. If it was manual, the +$1,576.51 says nothing about the strategy or the EA's trade management. Please confirm whether you closed it.

### 3.4 What the demo has not shown

| Not shown | Why it matters |
| --- | --- |
| Break-even, partial close and trailing on an engine trade | Break-even was seen once, on the matrix trade. The partial close at 1.2R and the ATR trail have no log line |
| A trade closed by the EA's own stop or trail | The only engine trade ended outside the EA |
| Partial fills, requotes, delayed feedback in live conditions | The README still lists these as unproven |
| An engine restart mid-trade | Fixed in code (`engine_runtime_state`) and tested, not run live |
| Rollover, swap, weekend gap | Not exercised; the trade closed the same day |
| Commission | MetaQuotes-Demo charged `0.0`; the replay assumes $7.00 per lot round trip (about $165 on a 23.53-lot trade) |
| More than one day of continuous operation | G1 needs 5 |

---

## 4. Strategy evidence (replay, 2022-12 to 2026-11, six pairs, M15)

| Measure | Result |
| --- | --- |
| Baseline, base cost | 5,622 development trades, **-0.35R** per trade, 95% CI -0.37 to -0.32, profit factor 0.47 |
| Holdout (last 6 months) | 784 trades, -0.36R |
| All 18 complete variants (coded defaults, pullback 2 pips, three exit modes, 1x and 1.5x cost) | -0.32R to -0.52R |
| Multiple-testing check (R3) | Every complete variant fails; 3 variants inconclusive because their trade files were overwritten |
| Before costs | About -0.05R. Random direction +0.01R, inverted direction +0.05R, so the entry rule has no directional information |
| Cost | About 0.3R per trade on the median 8.2-pip stop. Stops of 15 pips or more halve the loss (-0.17R) and stay negative |
| Authenticity | No lookahead found (1,800 samples). Reports carry a gate label; the 2026-11-03 batch predates the gate |
| Account halts on | The replay reaches the 20% drawdown halt within two weeks |

Monte Carlo on these trades at the locked 3.2% risk (block bootstrap, from the draft SRS v1.1 proposal): median ending equity 0.92 after 30 trades and 0.81 after 100; the 20% drawdown halt triggers in about 64% to 70% of 100-trade runs.

---

## 5. Updates needed

Items marked **protected** touch `mt5_ea/**`, `core/risk/**`, `config_microcapital.py` or `SRS_v1.md`, so they need a second-model review and your approval (AGENTS.md).

### P0: before the engine runs again

| # | Update | Why | Protected |
| --- | --- | --- | --- |
| 1 | Attach EA build `2026.11.04-1` (`scripts/mt5/compile_ea.sh --deploy`, then re-attach) | The chart still runs `-2`. The merged code expects `calendar_complete` in `status.json`, and `-3` fixes exit timestamps | Deploy is an owner action |
| 2 | Start the engine once so it consumes `exit_59159851261.json` | The DB still shows the GBPUSD trade as `EXECUTED_OPEN` | No |
| 3 | Install reviewed rate differentials | `readiness_preflight` reports BLOCK: rates 65 days old. A candidate file awaits review | No |
| 4 | Refresh the calendar before 2026-11-17 | Coverage ends then. The refresh agent is uninstalled | No |
| 5 | Decide how manual closes are handled during a test release | A manual close breaks R, win-rate and drift statistics. Record it or avoid it | No |

### P1: before any funded account

| # | Update | Why | Protected |
| --- | --- | --- | --- |
| 6 | Strategy redesign (plan Phase 2b), tested in replay with the R1 to R5 gate before any demo release | Replay expectancy is negative | No |
| 7 | EA guard that refuses a netting account | The EA never reads `ACCOUNT_MARGIN_MODE`; its position-ticket management assumes hedging | **Yes** |
| 8 | Symbol mapping or a documented plain-name requirement | The EA hard-codes `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`, `USDCHF`; a broker suffix breaks it silently | **Yes** |
| 9 | A real-account switch | `RequireDemoAccount = true` makes the EA refuse real accounts. Changing it must come with the pre-live gate that replaces SRS §12 | **Yes** (SRS v1.1) |
| 10 | SRS v1.1: replace the §12.2 average-R metric, settle D2, D3, O1 to O5, and define the pre-live gate that replaces the 30-day demo (ADR 0007) | The current gates are internally inconsistent | **Yes** |
| 11 | USD account requirement stated and checked | The bridge defaults the account currency to USD and sizing assumes it | No |
| 12 | Repeat the EA matrix and R7 fill reconciliation on the chosen broker's demo | Costs, spreads, filling mode and symbol names differ per broker | No |

### P2: structure and operations

| # | Update |
| --- | --- |
| 13 | Dashboard panels and alerts (Phase 4 S3 to S5): blocked entries, open reservations, conflicts, stale data |
| 14 | `run_replay` changes from the wave 2 note: separate output folder per batch, explicit variant fields in registry rows, run R3 after each batch |
| 15 | A Windows runtime check. `watchdog.py` is written for Windows Task Scheduler; the macOS launchd agents exist but are uninstalled |
| 16 | AI shadow components (Phase 5): the meta-labeler needs the Phase 2b strategy first |

---

## 6. Documents changed in this update

`docs/STATUS.md`, `AGENTS.md`, `fx_ai_engine/README.md`, a pointer at the top of `2026-11-03-system-assessment-and-benchmark.md` and the plan. New: this file and `2026-11-04-funded-account-and-broker-options.md`.
