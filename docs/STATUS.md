# Project status (living memory)

Updated: 2026-11-04 (validation waves and development wave merged; ADR 0007; capability and broker analysis). Every agent (Kiro, Claude Code, Codex) reads this before starting work and updates it when a phase, gate or known issue changes. Keep entries short and factual; put detail in the linked documents.

## Where the system is

| Item | State |
| --- | --- |
| Branch | `hardening/2026-11` (pushed, CI green). Development wave stages A to C and validation waves 1 to 4 are merged into it. `main` is behind; nothing has been merged into `main` yet. `dev/development-wave` (worktree `.worktrees/development-wave`) continues with the repository-interface work and is not yet merged past `97a8852`. |
| Plan | `fx_ai_engine/docs/plans/2026-11-03-full-development-and-ai-plan.md` (phases 0–7, gates G0–G6) |
| Decisions | `docs/decisions/`: 0001 plan defaults (demo account only); 0002 validation wave 1; 0003 development wave integration (one tool per job); 0004 live-vs-replay tools (wave 3); 0005 forward-test tooling (wave 4); 0006 multiple-testing (wave 2); **0007 no 30-day demo: continuous demo testing, one frozen release per owner-started test wave** |
| Current assessment | `fx_ai_engine/docs/2026-11-04-system-capability-and-readiness.md` (capability, demo record, update list). Funded accounts and brokers: `fx_ai_engine/docs/2026-11-04-funded-account-and-broker-options.md`. MT5, demo sufficiency and live-account feasibility: `fx_ai_engine/docs/2026-11-04-mt5-demo-and-live-account-assessment.md` |
| Account | MetaQuotes-Demo `5058284077`, balance 101,609.81 after one closed engine trade |
| Engine | Stopped. launchd agents (watchdog, calendar, keep-awake) uninstalled on 2026-11-03 at owner request |
| EA on chart | Build `2026.11.03-2`. Build `2026.11.04-1` (calendar completeness flag, plus build `-3`'s UTC exit times) is in the merged source and compiled in staging; the owner deploys it with `scripts/mt5/compile_ea.sh --deploy` and re-attaches. The copy in `MQL5/Experts` is the `-3` build |
| Tests | 581 passing on `hardening/2026-11` at `267fc41` (`581 passed in 45.78s`), `USE_MT5_MOCK=1 ../.venv/bin/python -m pytest -q` from `fx_ai_engine/`, Python 3.12 venv. Smoke mode runs. Policy hash unchanged: `f00f1f9c…535dc8` |

## Phase and gate status

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 Foundation | Done (G0 met) | AGENTS.md, CI workflow, Kiro hooks, ADR 0001, lock file |
| 1 Demo plumbing | Done except burn-in | File bridge 43/43 checks; EA matrix 7/7 cases (`fx_ai_engine/artifacts/demo-evidence/2026-11-03/`); first engine trade GBPUSD BUY 23.53 lots routed and filled. **Its exit (+1,576.51, 0.49R) was not placed by the EA** (exit deal magic 0, no EA log line); probable manual close, to be confirmed by the owner, so it is not strategy evidence. G1 burn-in paused after <1 day |
| 2 Edge evidence | Replay built; **G2 failed: no edge** | See "Phase 2 result" below |
| Dev wave A: points of error | Done, merged into `hardening/2026-11` | Known issues 1–5 below fixed with tests; long-job runbook `fx_ai_engine/docs/runbooks/long-jobs.md` |
| Dev wave B: integrate parallel work | Done, merged (owner approved the merge 2026-11-04; the EA change is calendar-export only). EA build `2026.11.04-1` still to be deployed | Research package and codex hardening merged; `run_replay` gates every report (R1/R2), adds R4 cost stress and R5 Monte Carlo; `backtesting/bias_checks.py` removed; ADR 0003 |
| Dev wave C: Phase 4 split | Done, merged | Contract tests committed first (`42d2331`: `tests/test_contract_engine.py` incl. golden decision record `tests/fixtures/contract/decision_cycle_golden.json`, `tests/test_contract_database.py`), then `database/db.py` split (`3bdadd9`) and `main.py` split (`108da00`); contract tests and golden file unchanged by the moves |
| Validation waves 1 to 4 | Done, merged (ADRs 0002, 0004, 0005, 0006) | R1 lookahead and warm-up audit, R2 authenticity gate, R3 multiple-testing, R4 cost stress, R5 Monte Carlo, R7 fill reconciliation, R8 drift monitor, R9 postmortem, R10 freeze and go/no-go; 11 Kiro skills `.kiro/skills/fx-*`. Real-data results: R3 fails every complete variant; R7, R8, R9 and R10 are INCONCLUSIVE at 1 closed trade |
| 2b Strategy redesign | Not started. **This is the path to profitability** | Replay expectancy is -0.35R; every variant tested loses. Test redesigns in replay (R1 to R5) before any demo release |
| 3 SRS v1.1 | Draft only: `fx_ai_engine/docs/2026-11-04-srs-v1.1-draft-proposal.md`. Open: D2 (ADR 0001 may settle it), D3, O1 to O5, and the pre-live gate that replaces SRS §12 | Needs Phase 2b numbers; D3 default says lower risk to 1.0–1.5% |
| 4 Structure/ops | S1, S2 done and merged; S3–S5 not started | S1: `main.py` 2,395 → 1,116 lines (composition root + Engine delegates) with `application/{decision_cycle,market_data_service,feedback_service,engine_runtime}.py`. S2: `database/db.py` 1,582 → 78-line façade over `database/connection.py`, `migrations.py`, `repositories/{trades,risk,config}.py`. Public imports unchanged |
| 5 AI shadow | Not started | Meta-labeler scaffolding exists in `ml/` |
| 6 30-day demo | **Dropped by the owner (ADR 0007).** Replaced by continuous demo testing: owner-started test waves, one frozen release each, evaluated with `research.forward_test.*`, R7, R8, R9 | Pre-live gate for real money is still to be defined in SRS v1.1 |
| 7 Funded account | Not started. Not recommended until a release passes the replay gate | See `2026-11-04-funded-account-and-broker-options.md` |

## Phase 2 result (replay, 2022-12 to 2026-11, 6 pairs, M15)

- Unified replay (`backtesting/unified_replay.py`) reuses live agents, PortfolioManager and HardRiskEngine; reproduces the live 2026-11-03 16:30 decisions exactly (parity test). Look-ahead check: 0 differences in 1,800 samples (old `bias_checks`, now replaced by R1 `research/bias_audit.py`). These results predate the R2 gate and are not gate-certified evidence.
- Current strategy: about 5,600 trades, **−0.35R per trade after costs** (95% CI −0.37 to −0.32); 6-month holdout −0.36R. All 18 completed variants (coded defaults, pullback 2, exits TRAIL/FIXED/HYBRID, cost 1x/1.5x) lose 0.32–0.51R.
- At zero cost the entry rule is −0.05R; random direction +0.01R; inverted direction +0.05R. The entries carry no directional information.
- Costs on the median 8.2-pip stop are about 0.3R per trade. Stops ≥15 pips halve the loss (−0.17R) but stay negative.
- With SRS account halts on, the replay hits the 20% drawdown halt within two weeks of the start.
- Registry of every run: `fx_ai_engine/artifacts/experiments/registry.jsonl`; diagnostics: `fx_ai_engine/artifacts/replay/2026-11-03_edge_diagnostics.json`; datasets (CSV untracked, manifests tracked): `fx_ai_engine/artifacts/datasets/mt5_2026-11-03/`.

## Known issues resolved in development wave stages A and B (merged into `hardening/2026-11` on 2026-11-04)

Numbers are kept so earlier references stay valid.

1. **File-bridge race at bar boundaries** (fixed). `FileBridgeConnection._load_bars` now accepts a bar export whose last bar equals `status.json`'s `latest_closed_bars` or is exactly one bar newer; older is still `bar export lags the terminal`, anything else is `bar export is more than one bar ahead of the terminal status`. The "latest closed bar ends in the future" check still rejects an unclosed bar. Tests: `tests/test_file_bridge_connection.py::test_fetch_ohlc_accepts_bar_export_one_bar_ahead_of_status`, `..._two_bars_ahead_...`, `..._misaligned_...`, `..._one_bar_ahead_export_that_has_not_closed`.
2. **Unconsumed legacy exit** (fixed in code; live DB still `EXECUTED_OPEN` until the owner's next engine start in a testing wave). `main._normalise_legacy_exit_close_time` converts exits without `close_time_server` (EA builds before `2026.11.03-3`) on the file bridge: close time from `deals.json` for the position (UTC), else payload time minus `server_utc_offset_seconds` (new `FileBridgeConnection.server_utc_offset_seconds()`); the original is kept as `close_time_reported`, the receipt dedupe key stays on the raw payload. Tested with a verbatim copy of the live payload (`tests/fixtures/exits/exit_59159851261.json`) on a temp bridge and DB: trade `CLOSED_WIN` +1,576.51 0.4926R, `close_time` `2026-11-03T19:03:16.520000+00:00`, reservation `CLOSED`, file deleted, second delivery deduped (`tests/test_legacy_exit_consumption.py`).
3. **Replay resource use** (fixed). `backtesting/replay_cache.py` caches per-symbol candidates as gzip JSON keyed by policy hash, CSV hashes, cost model, start/end, cache version and a fingerprint of the agent/indicator/replay code (`artifacts/replay/cache/`, git-ignored; `--no-cache` bypasses). Default workers 2 (`--workers`, capped by `FX_MAX_WORKERS`); a spawn `Pool` that is terminated on any parent exception, workers niced to ≥10 that exit within 2 s if the parent dies; `run_replay`/`edge_diagnostics` turn SIGTERM into a clean exit; new `run_replay --symbols`. Tests: `tests/test_replay_cache_and_workers.py`.
4. **Long foreground jobs** (fixed). `python -m validation.detached_job start|status|stop` runs a command in its own session and process group with niceness 10, `FX_MAX_WORKERS=2`, a 3 h default timeout, kills leftovers in the group, and writes the completion flag `done.json` (`completed`/`failed`/`timeout`/`stopped`). Runbook: `fx_ai_engine/docs/runbooks/long-jobs.md`. Tests: `tests/test_detached_job.py`.
5. **Restart re-evaluates the current bar** (fixed). The per-symbol last-seen M15 candle map is persisted in the new `engine_runtime_state` table (per evidence stream and account scope; `migrate_add_engine_runtime_state`) and seeded on `Engine.__init__`; malformed, unknown-symbol or future values are ignored; a DB failure only logs. Tests: `tests/test_engine_restart_state.py`.

7. **Parallel agent work** (integrated in stage B). Codex worktree changes applied from the read-only snapshot (calendar completeness certification in EA and `install_calendar`, stricter account/ticket/bar checks in `core/file_bridge_connection.py`, `tests/test_mt5_calendar_integrity.py`); `research/validation-wave1` cherry-picked (`research/` R1/R2/R4/R5, hypothesis cards, `.kiro/skills/fx-*`, ADR 0002). Keep/drop decisions in ADR 0003. Both source worktrees left untouched. Tests: `tests/test_run_replay_gate.py`, `tests/test_research_*.py`, `tests/test_mt5_calendar_integrity.py`.

## Known issues and points of error (open)

6. **Rate differentials stale** since 2026-08-31; candidate file awaiting owner review (`fx_ai_engine/data/source_records/market_context_2026-11-03_rates.candidate.json`).
8. **SRS acceptance gates inconsistent** (average R ≥ 2.0 over all trades is unreachable at 2.2R targets; 3.2% risk incompatible with the 15% drawdown gate). Resolve in SRS v1.1.
9. **Daily calendar refresh skips the completeness certification** (owner decision, ADR 0003). `validation/refresh_mt5_calendar.py` installs an archived copy, so the `calendar_complete` check (only for `market/calendar.json`) does not run on that path. Left unchanged so the refresh keeps working with EA `-2`/`-3`; recommended: verify the bridge `status.json` certification before archiving, shipped together with EA build `2026.11.04-1`.
10. **EA build `2026.11.04-1` not deployed.** Until it is attached, installing the bridge `market/calendar.json` directly with `validation.update_market_context` fails (`MT5 calendar query completeness is unverified`).
11. **Repository interfaces: in progress** on `dev/development-wave` (`application/repository.py`, memory and sqlite repositories). Application code on `hardening/2026-11` still calls `database.db` functions directly. Original note: **not built** (August audit §2, deferred). Application code still calls `database.db` functions directly; there is no in-memory repository for replay tests. Not needed for stage C's behaviour-preserving split.

12. **The only engine trade was not closed by the EA.** Position 59159851261 (GBPUSD): exit deal magic 0, empty comment, no EA break-even/partial/trail log line; R9 flags "possible manual close". Owner to confirm. Until confirmed, treat its +1,576.51 as unrelated to strategy or EA management. Rule for test waves: do not close engine trades by hand, or record each manual close so the statistics can exclude it.
13. **EA does not refuse a netting account.** It never reads `ACCOUNT_MARGIN_MODE`. Add a fail-closed guard before using any broker whose account type is not confirmed as hedging (protected path).
14. **EA symbol names are hard-coded without a suffix.** A broker with suffixed symbols breaks it. Verify on the broker's demo, then add a mapping if needed (protected path).
15. **`RequireDemoAccount = true`** makes the EA refuse real accounts. Intended. Turning it off needs owner approval and the SRS v1.1 pre-live gate (ADR 0007).
16. **Replay series lost for three variants.** The 2026-11-03 batches shared one output folder, so a later run overwrote the trade files of `rsi_bounded`, `adx_20_25` and `trend_distance_5pip`. R3 reports them as `SERIES_FILES_MISSING`. Fix: per-batch output folders in `run_replay.py` (wave 2 integration note (a)) and re-run those variants.
17. **Calendar coverage ends 2026-11-17** and the refresh agent is uninstalled. Refresh before the next engine run.
18. **Commission is 0.0 on MetaQuotes-Demo** while the replay assumes $7.00 per lot round trip. Demo P&L overstates results by that amount until a raw-spread broker demo is used.
19. **No live mode.** `main.py --mode` accepts only `smoke` and `demo`, demo refuses a non-demo account, and the EA's `RequireDemoAccount = true` drops signals on a real one. Going live is a deliberate build (protected paths, SRS v1.1 pre-live gate, owner approval). See `fx_ai_engine/docs/2026-11-04-mt5-demo-and-live-account-assessment.md`.
20. **Host is not fit for unattended or live running.** MacBook Pro M2, 8 GB: 4.9 GB swap in use, MT5 at about 140% CPU, sleeps when the display sleeps (keep-awake agent uninstalled). Real money needs a Windows VPS.
21. **MetaQuotes-Demo connection drops**: 21 losses on 2026-11-03 and 76 on 2026-11-04 (up to 08:43 local), each re-authorized within 1 to 2 s. Cause unknown (network, Wine or server); compare on a broker demo.
22. **Calendar export intermittently empty.** The EA exported 0 events at 10:03 UTC on 2026-11-04 (31 the day before). `install_calendar` rejects an empty list, so the installed calendar (to 2026-11-17) stays in use. Treat an empty export as a failed refresh in the EA (protected path).
23. **MetaQuotes-Demo prices are not market-like for cost testing**: zero commission, GBPUSD spread 0.0 pips. Do not use its P&L as a cost estimate.

## Operating rules learned

- One agent per working tree. Use a git worktree per task; never two agents in the main folder.
- Development and testing are separate waves. Do not leave long replays or the live engine running unattended unless the owner starts a test wave.
- Heavy jobs: detached, `nice`, at most 2–3 worker processes, a completion flag file, and a check that no child processes remain afterwards.
- Only the EA places orders. MT5 MCP trade tools stay blocked.
- Demo testing runs in owner-started test waves, one frozen release each (ADR 0007). Any change to code, config, policy or EA starts a new release; do not pool results across releases.
- Do not fund an account until a release passes the replay gate and SRS v1.1 defines the pre-live gate (`fx_ai_engine/docs/2026-11-04-funded-account-and-broker-options.md`).
- Test seams after the Phase 4 split: point tests at a temp DB by patching `database.connection.get_conn` or `database.connection.DB_PATH` (the `database.db` copies are not read at call time); patch alerts on `application.feedback_service` and `update_health_status` on `application.engine_runtime`. `main.insert_account_metrics` still covers `_record_consecutive_loss_update`.
