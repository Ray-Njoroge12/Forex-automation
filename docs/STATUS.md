# Project status (living memory)

Updated: 2026-11-04. Every agent (Kiro, Claude Code, Codex) reads this before starting work and updates it when a phase, gate or known issue changes. Keep entries short and factual; put detail in the linked documents.

## Where the system is

| Item | State |
| --- | --- |
| Branch | `hardening/2026-11` (pushed). `main` is behind; nothing merged yet. Development wave in progress on `dev/development-wave` (worktree `.worktrees/development-wave`, plan `.agents/tasks/development-wave/plan.md`); stages A and B done, not yet merged. |
| Plan | `fx_ai_engine/docs/plans/2026-11-03-full-development-and-ai-plan.md` (phases 0–7, gates G0–G6) |
| Decisions | `docs/decisions/0001-development-plan-defaults.md` (owner chose defaults; demo account only), `0002-validation-wave1-research-tooling.md`, `0003-development-wave-integration.md` (one tool per job, codex review) |
| Account | MetaQuotes-Demo `5058284077`, balance 101,609.81 after one closed engine trade |
| Engine | Stopped. launchd agents (watchdog, calendar, keep-awake) uninstalled on 2026-11-03 at owner request |
| EA on chart | Build `2026.11.03-2`. Build `-3` (exit close_time in UTC) compiled and deployed to `MQL5/Experts` but needs re-attaching. Build `2026.11.04-1` (adds `calendar_complete`) compiled in staging on `dev/development-wave` only; deploy after review + owner approval (ADR 0003) |
| Tests | 455 passing on `dev/development-wave` after stage B (`455 passed in 40.26s`; 388 after stage A, 353 at base), `USE_MT5_MOCK=1 ../.venv/bin/python -m pytest -q` from `fx_ai_engine/`, Python 3.12 venv |

## Phase and gate status

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 Foundation | Done (G0 met) | AGENTS.md, CI workflow, Kiro hooks, ADR 0001, lock file |
| 1 Demo plumbing | Done except burn-in | File bridge 43/43 checks; EA matrix 7/7 cases (`fx_ai_engine/artifacts/demo-evidence/2026-11-03/`); first engine trade GBPUSD BUY 23.53 lots routed, filled, closed +1,576.51 (0.49R). G1 burn-in started then paused after <1 day |
| 2 Edge evidence | Replay built; **G2 failed: no edge** | See "Phase 2 result" below |
| Dev wave A: points of error | Done on `dev/development-wave` (needs review + owner approval; touches `main.py`) | Known issues 1–5 below fixed with tests; long-job runbook `fx_ai_engine/docs/runbooks/long-jobs.md` |
| Dev wave B: integrate parallel work | Done on `dev/development-wave` (needs second-model review + owner approval; touches `mt5_ea/FX_Execution.mq5`) | Research package and codex hardening merged; `run_replay` gates every report (R1/R2), adds R4 cost stress and R5 Monte Carlo; `backtesting/bias_checks.py` removed; ADR 0003 |
| 2b Strategy redesign | Not started (required before Phase 6) | — |
| 3 SRS v1.1 | Not started | Needs Phase 2b numbers; D3 default says lower risk to 1.0–1.5% |
| 4 Structure/ops | Not started | `main.py` 2,300 lines, `db.py` 1,550 lines |
| 5 AI shadow | Not started | Meta-labeler scaffolding exists in `ml/` |
| 6 30-day demo | Blocked by 2b, 3, G1 | — |

## Phase 2 result (replay, 2022-12 to 2026-11, 6 pairs, M15)

- Unified replay (`backtesting/unified_replay.py`) reuses live agents, PortfolioManager and HardRiskEngine; reproduces the live 2026-11-03 16:30 decisions exactly (parity test). Look-ahead check: 0 differences in 1,800 samples (old `bias_checks`, now replaced by R1 `research/bias_audit.py`). These results predate the R2 gate and are not gate-certified evidence.
- Current strategy: about 5,600 trades, **−0.35R per trade after costs** (95% CI −0.37 to −0.32); 6-month holdout −0.36R. All 18 completed variants (coded defaults, pullback 2, exits TRAIL/FIXED/HYBRID, cost 1x/1.5x) lose 0.32–0.51R.
- At zero cost the entry rule is −0.05R; random direction +0.01R; inverted direction +0.05R. The entries carry no directional information.
- Costs on the median 8.2-pip stop are about 0.3R per trade. Stops ≥15 pips halve the loss (−0.17R) but stay negative.
- With SRS account halts on, the replay hits the 20% drawdown halt within two weeks of the start.
- Registry of every run: `fx_ai_engine/artifacts/experiments/registry.jsonl`; diagnostics: `fx_ai_engine/artifacts/replay/2026-11-03_edge_diagnostics.json`; datasets (CSV untracked, manifests tracked): `fx_ai_engine/artifacts/datasets/mt5_2026-11-03/`.

## Known issues resolved in development wave stages A and B (`dev/development-wave`, not merged)

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

## Operating rules learned

- One agent per working tree. Use a git worktree per task; never two agents in the main folder.
- Development and testing are separate waves. Do not leave long replays or the live engine running unattended unless the owner starts a test wave.
- Heavy jobs: detached, `nice`, at most 2–3 worker processes, a completion flag file, and a check that no child processes remain afterwards.
- Only the EA places orders. MT5 MCP trade tools stay blocked.
