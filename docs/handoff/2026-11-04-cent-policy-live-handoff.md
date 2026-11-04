# Handoff: Policy-Live track (C3 micro_live policy, C5 guarded live mode)
Date: 2026-11-04.

**Path note.** Moved to `docs/handoff/` (singular) at integration.

## Where
- Branch `cent/policy-live-track`, worktree `/Users/raynj/Desktop/Forex-automation/.worktrees/cent-policy-live`, base `71bb25fc3430323c195ee06a69671b7060741f46`. Not pushed, not merged.
- Commits (oldest first): `f76dc43` micro_live policy; `b77a150` approval schema and helpers; `fffd792` startup gate and per-loop checks; `827fa40` `--mode live` wiring in main.py; `b323f88` loss-streak reset command; `2287465` SRS addendum, ADR 0013 and this handoff; `d1a5914` live session closed on every exit and halt on loop side-effect failure; `00ef61a` reset command treats health-probe timeouts as indeterminate; `79b2502` preserve-divisor override ignored for micro_live (branch tip at review: `79b2502`).

## micro_live
`CORE_SRS_CONFIG` plus `MODE_ID` `micro_live`, `MODE_LABEL` `Micro live`, `EVIDENCE_LABEL` `Micro live percent-risk`, `BASE_RISK_PCT` 0.015. Registered in `MODE_CONFIGS` and `NON_SRS_MODE_IDS`, not in `OVERRIDE_ENABLED_MODE_IDS`.
- `core_srs` hash: `f00f1f9c3e9120971378f3c1eb95d28f09d1d5e2ee97035a7f4c00a998535dc8` (unchanged)
- `micro_live` hash: `53ff41104c05c4c6374ad59d62023575d648155ce190a507a7530c6e112f9489`

## Approval file schema (`fx_ai_engine/config/live_approval.json`, git-ignored)
`schema_version` (1), `approved_by`, `approved_at_utc`, `expires_at_utc`, `account_login`, `account_server`, `policy_mode` (`micro_live`), `policy_hash`, `strategy_id` (`trend_pullback_v1`), `ea_version_min`, `starting_balance_usd`, `max_total_loss_usd`, `max_orders_per_day`, `max_lot_per_order`, and `evidence {verdict_path, sha256}` or `evidence_waiver {reason, accepted_by}`. Template: `fx_ai_engine/config/live_approval.example.json`. Environment: `FX_LIVE_APPROVAL_FILE`, `FX_LIVE_CONFIRM=I-ACCEPT-LIVE-RISK-<login>`. Full table: the SRS addendum.

## main.py hunks (`git diff -U0` against base)
| Hunk | Function | Base lines | Final lines | Purpose |
| --- | --- | --- | --- | --- |
| H1 | imports | after 46 | 47 | `from core import live_gate` |
| H2 | `evaluate_runtime_policy_approval` | after 157 | 159-163 | a real account may run `micro_live` only when `live_gate.live_session_covers` is true |
| H3 | `parse_args` | 970 | 976 | `--mode` choices `smoke`, `demo`, `live` |
| H4 | `main()` | after 1030 | 1038-1049 | `startup_gate` before the health API and `Engine(...)`; return 2 on refusal; set `FX_POLICY_MODE=micro_live` |
| H5 | `main()` | after 1107 | 1126-1127 | `live_session.attach(engine)` before `engine.run`; return 2 on failure |

### Merge notes for dev/development-wave
- That branch rewrote the import block (lines 23-75), `_insert_risk_event_with_context`, `Engine.__init__` and the `Engine(...)` call. The hunks sit at least three context lines away, so expect at most adjacent-context conflicts in the import block (H1) and near the `Engine(...)` call (H5). Resolve by keeping both sides.
- H4 passes `running_strategy_id=globals().get("LIVE_STRATEGY_ID")`. It is `None` on base (treated as `trend_pullback_v1`) and picks up `main.LIVE_STRATEGY_ID` after the merge. `LiveSession.attach` also reads `getattr(engine, "strategy_id", LIVE_STRATEGY_ID)`.
- The per-loop hook wraps two attributes on the engine instance: `_enforce_execution_feedback_integrity` and `_record_consecutive_loss_update`. If the other branch renames either, the contingency is one line in `application/engine_runtime.run` after `engine._enforce_execution_feedback_integrity()`: `getattr(engine, "_live_gate_check", lambda: None)()`. Not needed on base.
- Mutation hazard: `--mode live` writes `os.environ["FX_POLICY_MODE"]`. Tests that call `main.main(["--mode", "live"])` must restore it (`monkeypatch.setenv("FX_POLICY_MODE", "")`).

## Contract assumptions
EA track (fixtures only): `status.json` fields `live_guard {account_real, require_demo, allow_live, max_lot, max_orders_per_day, orders_today, kill_switch}`, `account_margin_mode`, `account_currency`, `max_spread_pips_input`, `symbol_suffix`, `symbols_ok`, `symbol_errors`; file `<bridge root>/KILL_SWITCH`; EA version `2026.11.04-2`. The gate uses `live_guard`, `ea_version`, `login`, `server`, `account_is_demo`, `generated_at` and `schema_version`. Bridge track: money values in `account_snapshot.json` are dollars.

## Backtesting and research change list (not made; other session owns it)
- `backtesting/run_replay.py` line 196: hard-coded `core_srs`; add `--policy-mode` (default `core_srs`). Without it no PASS verdict carries the `micro_live` hash.
- `research/common.py` line 64 `policy_for_variant`: hard-coded `core_srs`; add an optional policy-mode argument (used by the authenticity gate, bias audit and cost stress CLIs).
- `research/monte_carlo.py`: `--risks 0.015` works today; add `--policy-mode` for completeness.
- `research/multiple_testing.py` line 118, `research/synthetic_demo.py`, `backtesting/edge_diagnostics.py` line 58: `core_srs` hard-coded; change only if used for `micro_live`.
- `research/forward_test/freeze.py` line 34 `POLICY_MODE = "core_srs"`: needs a parameter for a `micro_live` release.
- `backtesting/bt_runner.py`, `walk_forward.py`, `threshold_audit.py`, `walk_forward_suite.py` accept `--policy-mode micro_live`, but `simulation_profile.build_simulation_profile` labels any mode other than `preserve_10` as the smoke-test profile. Add a profile for a realistic run.
- `unified_replay` uses a 100,000 start balance and percent risk, so cent-lot granularity is not modelled.

## Observations
- `--mode smoke` on a real account is not blocked by Python for `core_srs`; only the EA inputs and the policy-hash pin guard it. For `micro_live` the non-SRS gate blocks it.
- Env-override quirk: `_mode_allows_runtime_overrides` reads `FX_POLICY_MODE`, not the policy being built. `get_policy_config("micro_live", env={"FX_POLICY_MODE": "preserve_10", "FX_ALLOW_NON_SRS_POLICY": "1", "MAX_SPREAD_PIPS": "9"})` returns spread 9. Pinned as current behaviour in `tests/test_micro_live_policy.py`. Fixing it touches every mode; proposed, not done.
- `validation.ea_demo_matrix --mode micro_live` works.
- The verdict value is `EVIDENCE_ELIGIBLE`, not "EVIDENCE".
- Per-loop version downgrade maps to `LIVE_GUARD_MISMATCH`; zero or non-finite equity maps to `LIVE_EQUITY_STALE`; an extra INFO `LIVE_STARTUP_APPROVED` audit row is written on a successful start.
- The real-Engine integration test (kill switch stops `_decision_cycle`) is in `tests/test_live_mode_main.py`, because it needs hunk H2.

## Operator commands (from `fx_ai_engine/`; none were run against the real terminal)
- Start: set `FX_LIVE_CONFIRM=I-ACCEPT-LIVE-RISK-<login>`, then `python main.py --mode live`.
- Kill switch: create the file `<bridge root>/KILL_SWITCH` (stops new entries, does not close positions); delete it to clear.
- Reset the loss-streak latch (engine stopped, after R7/R8/R9 review): `python -m validation.reset_loss_streak --account-login <N> --account-server "<server>" --operator "<name>" --reason "<text>"`, then type `RESET-LOSS-STREAK-<N>`.
- Hashes: `python -m validation.print_policy_hash --mode core_srs` and `--mode micro_live`.

## Open points for the owner
Risk value (1.5 % is proposed, not settled); caps `max_total_loss_usd`, `max_orders_per_day`, `max_lot_per_order`; evidence rule and waiver; `MAX_SPREAD_PIPS` 2.0 for cent spreads; deposits and withdrawals not tracked; approval of the SRS v1.1 addendum (DRAFT).

## Not verifiable without the real terminal
EA `live_guard`, `account_margin_mode`, `symbol_suffix` and `KILL_SWITCH` behaviour; the Bridge dollar normalisation of cent snapshots; `ACCOUNT_TRADE_MODE` on a real broker; end-to-end order placement; `HEALTH_PORT` behaviour on the operator's host (the reset tool's liveness probe).
