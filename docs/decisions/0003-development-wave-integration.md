# ADR 0003: Development wave stage B: integrating parallel work and one tool per job

Date: 2026-11-04. Status: accepted on `dev/development-wave`; merge needs a second-model review and owner approval (the EA is a protected path).

## Context

Two pieces of parallel work existed outside `hardening/2026-11`:

- Uncommitted changes in `.worktrees/codex-file-bridge-timestamp-validation` (base `8b05d2f`): calendar completeness certification in the EA and in `validation/update_market_context.py`, stricter account and ticket checks in `core/file_bridge_connection.py`, and tests. Taken from the read-only snapshot `.agents/tasks/development-wave/codex-uncommitted.patch` (sha `71cf481b…`, checked equal to the live worktree diff before applying) plus the untracked `tests/test_mt5_calendar_integrity.py`.
- Branch `research/validation-wave1`, commits `e829c66` and `fa466d8` on `4d7bec2`: the `research/` package (R1 bias audit, R2 authenticity gate, R4 cost stress, R5 Monte Carlo), five hypothesis cards, five `.kiro/skills/fx-*`, ADR 0002 and integration notes.

Both worktrees were left untouched. The research commits were cherry-picked (authorship and the ADR 0002 number kept); the codex patch was applied with `git apply --3way` on top of stage A.

## Decisions: one tool per job

| Job | Keep | Drop / change | Reason |
| --- | --- | --- | --- |
| Look-ahead and warm-up audit | `research/bias_audit.py` (R1) | Deleted `backtesting/bias_checks.py`. Its 2026-11-03 output `artifacts/datasets/mt5_2026-11-03/bias_checks.json` stays as historical evidence (STATUS "0 differences in 1,800 samples"). | R1 does everything `bias_checks` did and more: truncation **and** future-bar corruption (random and NaN), a decision fingerprint through Adversarial and PortfolioManager instead of only the candidate, blocking warm-up parity plus a warm-up deviation measurement. Both read CSV datasets (`--data-dir`). `bias_checks` had no tests and no other caller. |
| Replay engine | `backtesting/unified_replay.py` | none | The single engine. `research.common.run_replay_in_process` is a thin wrapper over its `generate_candidates` and `simulate`; every name research imports is still exported. |
| Authenticity verdict | `research/authenticity_gate.py` (R2) | none | New job with no overlap. Now called by `run_replay` for every result. |
| Cost stress | `unified_replay.COST_SCENARIOS` (simulation inputs) and `research/cost_stress.py` (R4 report: fixed-trade re-cost, re-run, break-even multiple) | No `stress_3x` scenario added. | One report tool. The scenarios stay what the simulation runs with; R4 answers "how much cost can these trades take" without another full replay. |
| Monte Carlo | `research/monte_carlo.py` (R5 `risk_sweep`) | `replay_report.monte_carlo_drawdown` stays only as R5's legacy i.i.d. reference kernel (docstring says so). `run_replay._section` now uses `risk_sweep`. | R5 adds the block bootstrap (keeps losing streaks), the SRS daily/weekly/drawdown/loss-streak halts and the risk grid. Two different Monte Carlo numbers in one report would be confusing. |
| Metrics and report rendering | `backtesting/replay_report.py` | `render_markdown` renders R4 and R5 sections instead of the old Monte Carlo line. | Used by R4 and `run_replay`; no second renderer. |
| Signal-versus-cost controls | `backtesting/edge_diagnostics.py` | none | Unique job: zero-cost, random-direction, inverted-direction and minimum-stop control rows. |

## Wiring in `backtesting/run_replay.py`

Applied as described in `fx_ai_engine/docs/research/validation-wave1-integration-notes.md` (a)–(e):

- After each `simulate`, `research.authenticity_gate.audit_replay` gates the result and `<stem>_gate.json` is written. The gate is bound to the report's `policy_hash` (the unmodified variant policy, also with `--halts edge`) and `dataset_sha256_16`.
- The report markdown starts with the gate's first line: `# Replay authenticity gate: PASS` or `# NOT EVIDENCE: replay failed the authenticity gate`.
- The report JSON and the registry row carry `gate_verdict` and `evidence_status`.
- Each section has `cost_stress` = `recost_trades(section_trades, frames, cost, (1.0, 1.5, 2.0, 3.0), scale_commission=True)` (absolute multiples, commission scaled like run_replay's profiles).
- Each section's `monte_carlo` = `risk_sweep` over trades sorted by `exit_time` with `exit_times=`, using the SRS policy (not the `--halts edge` overrides).
- R1 runs once per variant (policy) and is cached through `replay_cache` kind `r1_bias_audit`, keyed by policy hash, dataset hashes, symbols, start/end, sample size (60 per symbol) and seed (7), the candidate-code fingerprint and a fingerprint of `research/bias_audit.py`, `research/common.py`, `core/agents/adversarial_agent.py` and `core/agents/portfolio_manager.py`. On a cache miss R1 does one extra sequential candidate pass per symbol in the parent process; long runs should go through `validation.detached_job`.
- `--no-gate` skips R1 and R2; the gate file has verdict `SKIPPED`, the report starts with `# NOT EVIDENCE: authenticity gate skipped (--no-gate)`.
- `research` is imported inside the functions that use it, because `research.common` imports `run_replay.VARIANTS`.
- Report paths in the registry are relative to `fx_ai_engine/` when possible and absolute otherwise (`--out` outside the project used to crash).

Tests: `tests/test_run_replay_gate.py` (gate file, first line, registry fields, R4/R5 sections, R1 cache hit with `run_bias_audit` forbidden, `--no-gate`).

## Codex review outcome

Accepted as written, with one addition (the EA version bump):

- `_parse_utc` raises `FileBridgeDataError("invalid timestamp")` instead of a bare `ValueError`, so a malformed timestamp fails closed through the normal bridge error path.
- The account snapshot is accepted only when its login, server and demo flag match `status.json`, the account matches the one `connect()` saw, and `account_authenticated` and `terminal_connected` are true. The live build `2026.11.03-2` already writes these fields, so the live snapshot passes.
- `connect()` requires an integer login and a non-empty server. Position tickets must be positive integers with no duplicates and must match `open_positions_count`. `latest_closed_bars` entries must be dicts, and bar times positive integers (booleans are rejected).
- Stage A's `_load_bars` rule (accept the bar equal to the status bar or exactly one bar newer) is unchanged; the patch applied without conflicts next to it.
- EA: `CalendarValueHistory` returns the number of values or -1 ([MQL5 docs](https://www.mql5.com/en/docs/calendar/calendarvaluehistory)). The EA now treats a negative or mismatched count, or a failed `CalendarEventById`, as an incomplete refresh, keeps the previous `calendar.json`, and writes `calendar_complete: false` to `status.json`. Before, a currency with zero events counted as a failure, and a partial failure with at least one event was published as complete.
- `install_calendar` installs a bridge `market/calendar.json` only when `status.json` certifies it: `calendar_complete` true, connected demo terminal, matching event count, generated less than 2 hours ago.
- `EA_VERSION` bumped from `2026.11.03-3` to `2026.11.04-1` because `status.json` gains a field and evidence files identify builds by version. Compiled with `scripts/mt5/compile_ea.sh` (staging, no `--deploy`): 0 errors, 0 warnings.

Effect the owner will see: until build `2026.11.04-1` is attached, `status.json` has no `calendar_complete`, so installing directly from the bridge `market/calendar.json` with `validation.update_market_context` fails with `MT5 calendar query completeness is unverified`.

## Owner actions and open decision

1. **Deploy EA build `2026.11.04-1`.** Protected path: needs the second-model review and owner approval, then `scripts/mt5/compile_ea.sh --deploy` by the owner and re-attaching the EA. This also delivers build `-3`'s UTC exit times.
2. **Open decision: the daily calendar refresh bypasses the certification.** `validation/refresh_mt5_calendar.py` copies the bridge file to `data/source_records/market_context_<date>_mt5_calendar.json` and installs that copy, and the certification only runs for a file named `calendar.json` in a `market/` folder. Chosen now: leave this unchanged, so the daily refresh keeps working with builds `-2`/`-3` on the chart. Recommended follow-up: check the bridge `status.json` certification in `refresh_mt5_calendar` before archiving. That makes the daily refresh fail until build `2026.11.04-1` is attached, so it should land together with the EA deployment.
