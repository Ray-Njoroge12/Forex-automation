# ADR 0013: Micro-live policy and live gate
Date: 2026-11-04. Status: proposed. Needs second-model review and owner approval, because it changes `config_microcapital.py` and `fx_ai_engine/main.py`. ADR numbers 0008 to 0010 are reserved and not used here.
## Context
The owner wants to grow a small cent account (about 10 dollars) with the system. SRS v1 fixes 3.2 % risk per trade. `fx_ai_engine/docs/2026-11-04-ten-dollar-growth-pathway.md` (`output_growth_requirements.txt`) simulates 3.2 % against 1.5 %: the account halts in about 95 % of paths at 3.2 % and about 11 % at 1.5 %. ADR 0001 D3 already names 1.0 to 1.5 % as the conditional alternative. SRS v1 section 12 forbids live deployment before a 30-day demo, and ADR 0007 dropped that demo for development without defining a replacement gate. Nothing in the code stops `--mode smoke` on a real account for `core_srs`; only the EA inputs guard it.
## Decision
1. Add policy mode `micro_live`: `CORE_SRS_CONFIG` with four overrides (`MODE_ID` `micro_live`, `MODE_LABEL` `Micro live`, `EVIDENCE_LABEL` `Micro live percent-risk`, `BASE_RISK_PCT` 0.015). Everything else is inherited. `CORE_SRS_CONFIG` is untouched, hash `f00f1f9c3e9120971378f3c1eb95d28f09d1d5e2ee97035a7f4c00a998535dc8`. The `micro_live` hash is `53ff41104c05c4c6374ad59d62023575d648155ce190a507a7530c6e112f9489`. The label text is part of the hash.
2. `MAX_SPREAD_PIPS` stays 2.0, matching the EA input default.
3. No override mechanism for the risk value. `micro_live` is not in `OVERRIDE_ENABLED_MODE_IDS`, so an environment variable cannot move a hashed, approved number.
4. A known quirk is documented, not fixed: the override check reads `FX_POLICY_MODE` from the environment, not the policy being built. The live gate defends by requiring approval hash, pristine policy hash and running policy hash to be equal, and `--mode live` forces `FX_POLICY_MODE=micro_live`.
5. `micro_live` is a non-SRS mode. On mock or demo it needs `FX_ALLOW_NON_SRS_POLICY=1`. On a real account it runs only through `--mode live` after the startup gate passed.
6. New `--mode live` refuses to start unless a time-limited approval file, the confirmation text, matching account, policy hash, strategy id, EA version, EA `live_guard` and an evidence route all check out. Refusals return exit code 2, persist a risk event and write nothing to the bridge folder.
7. Evidence accepted: R2 authenticity-gate verdict `PASS` with `evidence_status` `EVIDENCE_ELIGIBLE` (not "EVIDENCE"), sha256 of the file and policy hash bound. `backtesting/run_replay.py` hard-codes `core_srs`, so no such verdict can carry the `micro_live` hash today. A loud, owner-signed waiver is the only route until that changes.
8. The strategy id is `trend_pullback_v1`, compared through `globals().get("LIVE_STRATEGY_ID")` so the plug-in branch needs no edit at merge time.
9. A loss-streak halt is latched in the database. It survives a later win and a restart. Only `python -m validation.reset_loss_streak` clears it, and only with the engine stopped.
10. The loss limit uses account equity in dollars: `starting_balance_usd - equity >= max_total_loss_usd` halts for the process. A stale or zero equity halts as `LIVE_EQUITY_STALE` and never trips the loss latch.
11. The per-loop check is installed by wrapping two methods on the engine instance. `application/**` is not edited. A kill-switch file stops new entries and never closes positions.
## OPEN POINTS for the owner
| Point | State |
| --- | --- |
| Exact risk value | **OPEN. 1.5 % is a proposed default, not settled.** ADR 0001 D3 says 1.0 to 1.5 %. A change gives a new hash and needs a new approval. |
| Caps `max_total_loss_usd`, `max_orders_per_day`, `max_lot_per_order` | **OPEN.** The example file holds placeholders only (3.0 dollars, 4, 0.01). The EA caps must be equal or stricter. |
| Evidence rule and waiver | **OPEN.** Whether a waiver is acceptable while no `micro_live` verdict can be produced. |
| `MAX_SPREAD_PIPS` 2.0 for cent-account spreads | **OPEN.** Spreads of 1.5 to 2.5 pips will reject some signals. A change is a new hash. |
| Deposits and withdrawals | **OPEN.** The loss limit does not track them. |
| Dollar contract from the Bridge track | **OPEN.** Equity is assumed to arrive in dollars; not verified on the real terminal. |
## Consequences
- A real-account run needs a new owner-written file, short expiry and a restart on renewal.
- With a waiver, live starts without replay evidence and the engine says so every 4 hours.
- Spread-gated rejections are likely on cent-account spreads.
- The current strategy loses about 0.32 to 0.36R per trade after costs in replay (ADR 0006). The gate limits damage; it does not create edge.
- `main.py` gets five isolated hunks; merges against `dev/development-wave` touch different lines.
## What does not change
- SRS v1 locked constants and the `core_srs` hash. `tests/test_srs_locked_constants.py` and `tests/test_ea_policy_pin.py` are unchanged.
- The AGENTS.md invariants: only the EA places orders, the Hard Risk Engine runs last, AI is shadow-only, every risk event is persisted with a reason code.
- `SRS_v1.md` is not edited. The addendum is a DRAFT: `fx_ai_engine/docs/2026-11-04-srs-v1.1-pre-live-gate-proposal.md`.
