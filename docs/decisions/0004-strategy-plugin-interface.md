# ADR 0004: Strategy plug-in interface and the Phase 2b hypotheses

Date: 2026-11-04. Status: accepted on `dev/development-wave` (stage D); merge needs a different-model review and owner approval (`main.py` and `application/decision_cycle.py` are guardrail paths). Design: `fx_ai_engine/docs/plans/2026-11-04-stage-d-strategy-plugin-design.md` (owner-approved revision 2).

## Context

Phase 2 showed the current entry rules carry no directional information (zero-cost −0.05R vs random +0.01R) and lose about 0.35R per trade after costs, mostly from about 0.3R of costs on median 8-pip stops. New entry rules had to be testable without a second copy of the decision code, and the live engine and the replay had to keep calling the same functions (AGENTS.md invariant 4).

## Decisions

1. **One strategy object replaces the Regime and Technical stages** (`fx_ai_engine/core/strategy/`). It returns a `StrategyDecision` (regime, REGIME pass/reject with reason and details, signal or reject reason). Session and news gates, Adversarial, PortfolioManager, the AI shadow layer, the Hard Risk Engine, reservation and routing are unchanged and run after it in the same order.
2. **Strategy id and params are arguments, not policy keys.** The `core_srs` policy dict is unchanged, so `canonical_policy_hash` and the EA's policy pin are unchanged (`f00f1f9c…35dc8` before and after). Replay records `strategy_id` and canonical params in the candidate and R1 cache keys, the report, the gate verdict and the registry row; trades carry a `strategy_id` column.
3. **One registry, always guarded.** `build_strategy` is the only constructor for live, replay, R1/R2, edge diagnostics and tests, and it always returns `GuardedStrategy`. The guard owns the SRS minimum R:R (SRS §5.3) for every strategy, plus symbol, direction, finite positive stop/target, MARKET orders only (the replay has no LIMIT fill model) and valid trade-management fields. Min R:R in the Hard Risk Engine stays a backlog item (`core/risk/**` is protected).
4. **Live runs `trend_pullback_v1` only.** `main.LIVE_STRATEGY_ID = DEFAULT_STRATEGY_ID`, with no CLI flag or environment variable. `trend_pullback_v1` wraps `RegimeAgent` + `TechnicalAgent` exactly as before; `core/agents/**` is not edited. The golden decision record and the 2026-11-03 16:30 live-parity test are unchanged.
5. **Live eligibility is enforced in code** (owner decision 2026-11-04). Every registration declares `live_eligible`; False needs a reason. `build_strategy(run_context="live")` refuses an ineligible or unknown id before the factory runs, and the first statement of `Engine.__init__` is `require_live_eligible(LIVE_STRATEGY_ID)` (CRITICAL log `STRATEGY_NOT_LIVE_ELIGIBLE`, then re-raise; nothing touches the bridge or DB). `d1_breakout_v1` (H-009) is registered `live_eligible=False` until an SRS v1.x revision of the timeframe model (§5.2) and stop basis (§5.3) is approved; its replay reports say "Research only" on line 2. `tests/test_strategy_live_eligibility.py` pins the eligible set.
6. **Regime cache is a replay option.** Replay passes `regime_cache_key = closed-H1 count`; live passes None, so live output (including `timestamp_utc`) is unchanged.
7. **Live strategy exceptions fail closed per symbol.** Before stage D an exception in `RegimeAgent`/`TechnicalAgent` stopped the engine loop (and with it exit-feedback consumption). Now the guard logs it at ERROR with traceback, the symbol gets REGIME REJECT `STRATEGY_ERROR`, TECHNICAL SKIP and a WARN risk event `STRATEGY_ERROR`, and the loop continues; the next M15 bar retries. Replay and tests use `on_error="raise"`.
8. **Contract for every strategy** (`tests/strategy_contract.py::assert_strategy_contract`): typed outputs, determinism, truncate-and-compare (completed bars only), fetches ≤ 400 bars (the EA export), and guard rules on signals. A static test forbids `core/strategy` from importing MetaTrader5, the bridge, the file-bridge connection or the database, and from reading the clock outside `base.new_trade_id`/`base.utc_now_iso` (trade ids and record timestamps only).
9. **Approved hypotheses implemented as code, no replays** (owner decision 2026-11-04): H-006 `london_breakout_v1`, H-007 `h1_channel_breakout_v1`, H-009 `d1_breakout_v1`; H-008 `range_fade_v1` dropped. Family size k = 3, Bonferroni z = 2.39. Cards with the pre-registered plan: `fx_ai_engine/docs/research/hypotheses/H-006…`, `H-007…`, `H-009…`. Swap allowance for the evaluation: `research/swap_allowance.py`.

## Implementation details not spelled out in the design

- `core/strategy/base.py` has two clock helpers, `new_trade_id()` and `utc_now_iso()` (the `timestamp_utc` of signals and of the `STRATEGY_ERROR` regime). Both fields are excluded from every comparison.
- H-006: the Asian range needs every M15 bar of [00:00, 07:00) London; the expected count is the elapsed time / 15 min (28 bars normally, 24 or 32 on the two UK DST-change days).
- H-007: the channel uses closed H1 bars only, so an M15 bar closing on the hour lies inside the last channel bar; triggers occur at :15, :30 and :45 (stated in the card).
- H-007 rejects a non-trending regime at the REGIME stage with reason `HCB_REGIME_NOT_TRENDING`; H-006 and H-009 accept every regime except `REGIME_DATA_UNAVAILABLE`/`REGIME_WARMUP_INCOMPLETE` and reject against-trend breakouts at the TECHNICAL stage.
- H-009 validation ranges for `min_stop_pips` [10, 500] and `max_stop_pips` [20, 1000] (the design gave defaults only). The rule "first session decision" also rejects a decision time outside `is_tradeable_session` (`D1T_OUTSIDE_SESSION`); this only narrows the session window.
- Swap allowance: rollovers counted Monday–Friday at 17:00 New York, Wednesday ×3 (7 nights per week).
- `research.bias_audit.observe_fetch_lengths` builds the strategy with its recording fetch; for `trend_pullback_v1` it still forces a trending regime when the regime rejects, so the Technical fetches are observed as before.

## Consequences

- New strategies are added by one module that calls `register_strategy`, a card, and tests that call `assert_strategy_contract`; the candidate cache fingerprint covers `core/strategy/**`.
- Running anything other than `trend_pullback_v1` live is an owner decision with an SRS v1.x change and an edit to the pinned eligibility test.
- Not done in this wave: replays of the hypotheses (testing wave), the news blackout in replay, a strategy-id DB column, min R:R in the Hard Risk Engine, and the README rule 3 change for H-001…H-005 (noted in the README).
