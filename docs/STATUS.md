# Project status (living memory)

Updated: 2026-11-04. Every agent (Kiro, Claude Code, Codex) reads this before starting work and updates it when a phase, gate or known issue changes. Keep entries short and factual; put detail in the linked documents.

## Where the system is

| Item | State |
| --- | --- |
| Branch | `hardening/2026-11` (pushed). `main` is behind; nothing merged yet. |
| Plan | `fx_ai_engine/docs/plans/2026-11-03-full-development-and-ai-plan.md` (phases 0–7, gates G0–G6) |
| Decisions | `docs/decisions/0001-development-plan-defaults.md` (owner chose defaults; demo account only) |
| Account | MetaQuotes-Demo `5058284077`, balance 101,609.81 after one closed engine trade |
| Engine | Stopped. launchd agents (watchdog, calendar, keep-awake) uninstalled on 2026-11-03 at owner request |
| EA on chart | Build `2026.11.03-2`. Build `-3` (exit close_time in UTC) compiled and deployed to `MQL5/Experts` but needs re-attaching |
| Tests | 353 passing (`USE_MT5_MOCK=1 ../.venv/bin/python -m pytest -q` from `fx_ai_engine/`), Python 3.12 venv |

## Phase and gate status

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 Foundation | Done (G0 met) | AGENTS.md, CI workflow, Kiro hooks, ADR 0001, lock file |
| 1 Demo plumbing | Done except burn-in | File bridge 43/43 checks; EA matrix 7/7 cases (`fx_ai_engine/artifacts/demo-evidence/2026-11-03/`); first engine trade GBPUSD BUY 23.53 lots routed, filled, closed +1,576.51 (0.49R). G1 burn-in started then paused after <1 day |
| 2 Edge evidence | Replay built; **G2 failed: no edge** | See "Phase 2 result" below |
| 2b Strategy redesign | Not started (required before Phase 6) | — |
| 3 SRS v1.1 | Not started | Needs Phase 2b numbers; D3 default says lower risk to 1.0–1.5% |
| 4 Structure/ops | Not started | `main.py` 2,300 lines, `db.py` 1,550 lines |
| 5 AI shadow | Not started | Meta-labeler scaffolding exists in `ml/` |
| 6 30-day demo | Blocked by 2b, 3, G1 | — |

## Phase 2 result (replay, 2022-12 to 2026-11, 6 pairs, M15)

- Unified replay (`backtesting/unified_replay.py`) reuses live agents, PortfolioManager and HardRiskEngine; reproduces the live 2026-11-03 16:30 decisions exactly (parity test). Look-ahead check: 0 differences in 1,800 samples.
- Current strategy: about 5,600 trades, **−0.35R per trade after costs** (95% CI −0.37 to −0.32); 6-month holdout −0.36R. All 18 completed variants (coded defaults, pullback 2, exits TRAIL/FIXED/HYBRID, cost 1x/1.5x) lose 0.32–0.51R.
- At zero cost the entry rule is −0.05R; random direction +0.01R; inverted direction +0.05R. The entries carry no directional information.
- Costs on the median 8.2-pip stop are about 0.3R per trade. Stops ≥15 pips halve the loss (−0.17R) but stay negative.
- With SRS account halts on, the replay hits the 20% drawdown halt within two weeks of the start.
- Registry of every run: `fx_ai_engine/artifacts/experiments/registry.jsonl`; diagnostics: `fx_ai_engine/artifacts/replay/2026-11-03_edge_diagnostics.json`; datasets (CSV untracked, manifests tracked): `fx_ai_engine/artifacts/datasets/mt5_2026-11-03/`.

## Known issues and points of error (open)

1. **File-bridge race at bar boundaries.** `FileBridgeConnection._load_bars` rejects a bar file whose last bar is *newer* than `status.json` (status written every 5 s, bars on bar close). Caused missed decisions on 2026-11-03 (8 `bar export lags the terminal` errors with last > expected).
2. **Unconsumed exit.** `exit_59159851261.json` (GBPUSD close, written by EA build -2 with server time labelled `Z`) is still in the bridge; DB still shows `EXECUTED_OPEN`. The engine must consume it on next start before any new trading.
3. **Replay resource use.** Candidate generation re-runs per variant (≈2.5 min × 6 processes); killed parents left orphaned `multiprocessing` workers. Needs candidate caching, a lower default worker count, and clean shutdown.
4. **Long foreground jobs.** Agent tool timeouts killed foreground replays. Long jobs must run detached with a completion flag and a resource budget.
5. **Restart re-evaluates the current bar.** An engine restart mid-bar re-runs that bar's decisions (blocked by portfolio checks on 2026-11-03, but should be fixed).
6. **Rate differentials stale** since 2026-08-31; candidate file awaiting owner review (`fx_ai_engine/data/source_records/market_context_2026-11-03_rates.candidate.json`).
7. **Parallel agent work.** `.worktrees/codex-file-bridge-timestamp-validation` (uncommitted file-bridge/calendar hardening) and branch `research/validation-wave1` (research tooling: bias audit, authenticity gate, cost stress, Monte Carlo, hypothesis cards, Kiro skills) are not reviewed or merged and overlap with work on `hardening/2026-11`.
8. **SRS acceptance gates inconsistent** (average R ≥ 2.0 over all trades is unreachable at 2.2R targets; 3.2% risk incompatible with the 15% drawdown gate). Resolve in SRS v1.1.

## Operating rules learned

- One agent per working tree. Use a git worktree per task; never two agents in the main folder.
- Development and testing are separate waves. Do not leave long replays or the live engine running unattended unless the owner starts a test wave.
- Heavy jobs: detached, `nice`, at most 2–3 worker processes, a completion flag file, and a check that no child processes remain afterwards.
- Only the EA places orders. MT5 MCP trade tools stay blocked.
