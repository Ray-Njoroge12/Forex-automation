# Stage D design: strategy plug-in interface and Phase 2b hypotheses

Date: 2026-11-04. Worktree `/Users/raynj/Desktop/Forex-automation/.worktrees/development-wave` (branch `dev/development-wave`, stages A–C committed, HEAD `d760534`). Development only: no live trading, no long replays, no model training. Status: **approved by the owner on 2026-11-04** (H-006, H-007, H-009 approved; H-008 dropped; section 10). Revision 2 adds the code-enforced live-eligibility rule the owner asked for (section 3.5a).

## 1. Overview

Phase 2 showed that the current entry rules carry no directional information and that costs on their tight stops take about 0.3R per trade. Stage D does two things. First, it puts the Regime and Technical stages behind one strategy object, built from one registry, so the live engine and the replay call the same code (AGENTS.md invariant 4). The current rules become the strategy `trend_pullback_v1`, which stays the only strategy the live engine can run. Second, it implements three new strategy hypotheses approved by the owner (H-006, H-007, H-009), each derived from a specific Phase 2 number. A fourth proposal (H-008) was dropped by the owner. Each hypothesis is coded as a module with unit tests and fixture tests, and each gets a hypothesis card that fixes its parameters and its decision rule before any replay runs. The replays themselves belong to the later testing wave.

Nothing in this design changes `config_microcapital.py`, `SRS_v1.md`, `core/risk/**`, `bridge/**`, `mt5_ea/**` or `core/agents/**`. The `core_srs` policy dict gains no keys, so `canonical_policy_hash` and the EA's policy pin stay the same. One hypothesis (H-009) needs an SRS timeframe and stop-basis revision before it could ever run live. Until the owner approves that revision, it is replay/research only, and the code refuses to start the live or demo engine with it (section 3.5a).

Technology stack (locked once approved): Python 3.12 from `/Users/raynj/Desktop/Forex-automation/.venv`, pandas 2.3.3, numpy 1.26.4, the existing `core/indicators` functions, `zoneinfo` for session clocks, pytest. No new dependencies.

## 2. Phase 2 evidence used

Sources: `fx_ai_engine/artifacts/replay/2026-11-03_edge_diagnostics.json` (tracked; development period 2022-12-24 to 2026-05-03, fixed sizing, account halts off), `docs/STATUS.md` "Phase 2 result", and the development-section trade lists in the main checkout `fx_ai_engine/artifacts/replay/2026-11-03_edge/*_trades.csv` (untracked). Rows marked "derived" are my aggregation of those CSVs for this design (entries before 2026-05-03 only; `gross_r = gross_usd / risk_usd`, which includes spread and slippage but not commission). None of these results passed the R2 authenticity gate (they predate it), so they motivate hypotheses but are not evidence in the R2 sense.

| # | Finding | Numbers |
| --- | --- | --- |
| E1 | Entries have no directional information | Zero cost, fixed 2.2R: −0.0502R (95% CI −0.1014 to −0.0001), 3,642 trades, win rate 27.9%. Break-even win rate at that payoff (avg winner 2.448R, avg loser −1.015R) is 29.3%. |
| E2 | Direction controls | RANDOM direction +0.0075R (CI −0.041 to +0.059, 3,788 trades). INVERTED +0.052R (CI +0.004 to +0.100, 3,933 trades): the only row whose CI excludes zero. |
| E3 | Costs dominate | Base cost fixed 2.2R: −0.361R (4,039 trades, win rate 22.5%); live exits −0.347R (5,622). Cost ≈ 0.31R per trade: commission 0.105R (`mean_cost_r` in the JSON is commission only), spread + slippage ≈ 0.21R (derived: gross_r −0.256 vs −0.050 at zero cost). |
| E4 | Cost scales with 1/stop; wide stops alone do not create edge | Stop quartiles 6.14 / 8.49 / 12.0 pips. Derived, base cost fixed 2.2R by stop: ≤6 pips −0.526R (1,055 trades), 6–8 −0.403 (801), 8–10 −0.328 (651), 10–15 −0.278 (927), 15–20 −0.191 (325), >20 −0.172 (280). JSON: stop ≥15 pips −0.166R (1,175), ≥20 −0.161R (564). Before commission, >20-pip trades are still −0.139R. |
| E5 | Live exits cap winners | Live exits (TRAIL, break-even at 0.8R, 50% partial at 1.2R) at zero cost: win rate 45.9%, avg winner 1.000R, −0.0185R. ADR 0001 D2 needs avg winner ≥ 1.5R. Derived, fixed-TP trades: median max favourable excursion 0.49R; 36% reach 1R; 9.6% reach 2.2R. |
| E6 | Low volatility is worst | Derived, base fixed 2.2R: LOW −0.463R (979), NORMAL −0.322R (2,634), HIGH −0.367R (426). Consistent with E4 (low ATR, small stops). |
| E7 | Entries fire on a third of trending session bars | Candidate funnel (baseline, full period): 46,195 candidates out of 141,438 trending-regime session bars (33%). The pullback/RSI check passes 83% of the bars that reach it (16,252 rejects); with the active 20-pip buffer and RSI 0–100 it barely filters. |
| E8 | Re-tuning the current family does not help | All 18 completed variants lose 0.32–0.51R. `coded_defaults` (tighter filters): −0.342R (1,777 trades, derived). H-001 to H-004 already cover this space. |
| E9 | Hour-of-day is not stable | Development 13–14 UTC −0.18 / −0.14R vs 10 UTC −0.57R; holdout 14 UTC −0.46R. Not used as a basis (section 5.6). |

What follows from this: a new strategy must (a) choose its direction from a specific market event, not from "any bar in a trend" (E1, E7); (b) use stops wide enough that costs fall to about 0.1R (E3, E4); a round trip costs about 2.5 pips (spread floor 0.8–1.5 pips, 2 × 0.2 pips slippage, $7/lot commission ≈ 0.7 pip), so 20 pips ≈ 0.12R and 30 pips ≈ 0.08R; (c) keep winners at the full target (E5); and (d) beat its own RANDOM-direction control, not only the baseline (E2).

## 3. Strategy plug-in interface

### 3.1 Where it sits in the decision flow

The strategy object replaces exactly the Regime and Technical boxes of the AGENTS.md flow. Everything after it is unchanged and runs in the same order:

```
closed bars (EA export) -> [ Strategy: Regime -> Technical -> SignalGuard ] -> Adversarial -> Portfolio Manager
  -> [AI veto layer, shadow only] -> Hard Risk Engine -> reservation -> Signal Router -> pending_signals/ -> EA
```

Session and news gates stay where they are today (live: `run_decision_cycle` and `evaluate_symbol` before the strategy; replay: `is_tradeable_session` in `generate_candidates`). A strategy may narrow the session window, never widen it. The strategy returns a candidate signal or a rejection; it never sizes, routes or approves anything. The Hard Risk Engine still runs last and has the final say (invariant 2). PortfolioManager sizing keeps using `regime.atr_ratio` and `volatility_state`, and every strategy supplies them from the H1 `RegimeAgent`.

### 3.2 Alternatives considered

- **Strategy as policy overrides** (more `AGENT_THRESHOLDS` or `EXPERIMENTS` keys). Rejected: it cannot express new entry logic, and every new key changes the EA-pinned policy hash.
- **Strategy owns the whole pipeline** (including Adversarial and PortfolioManager). Rejected: a strategy could then skip downstream gates, and the gate code would be duplicated.
- **Plug in at the Technical stage only**, with the engine always running `RegimeAgent`. Simpler, but then the REGIME funnel stage cannot express a strategy's own regime requirement (H-006 and H-009 accept any regime except a counter-trend one; a range strategy would need flat regimes), and the replay's per-H1 regime cache would have to move into the engine.
- **Chosen: one object for Regime + Technical** (plan D1), returning a typed `StrategyDecision`, built only through the registry and always wrapped in a guard. All new strategies use `RegimeAgent` through a shared helper, so the H1 regime classification (SRS §8.3) is the same for every strategy.

### 3.3 Types (`fx_ai_engine/core/strategy/base.py`)

```python
FetchOHLC = Callable[[str, int, int], pd.DataFrame]   # (symbol, timeframe code, bars) -> closed bars
FetchSpread = Callable[[str], float | None]           # price units
LIVE_BAR_EXPORT_LIMIT = 400                           # FX_Execution MarketBarsExport

@dataclass(frozen=True)
class StrategyDecision:
    strategy_id: str
    regime: RegimeOutput
    regime_passed: bool          # REGIME funnel outcome PASS/REJECT
    regime_reason_code: str      # REGIME funnel reason (default strategy: regime.reason_code)
    regime_details: str
    signal: TechnicalSignal | None
    reason_code: str             # TECHNICAL funnel reason; "TECH_SKIPPED_REGIME_REJECTED" when regime_passed is False
    details: str

class Strategy(Protocol):
    strategy_id: str
    symbol: str
    def evaluate(self) -> StrategyDecision: ...
```

`evaluate()` takes no time argument. Strategies read bars only through the injected `fetch_ohlc`/`fetch_spread`. A strategy that needs a clock (H-006, H-009) takes it from the bars: decision time = open time of the last closed M15 bar + 15 minutes. That clock is identical in live and replay, and it makes the truncate-and-compare test meaningful. `trade_id` and `timestamp_utc` keep using the wall clock, through one helper `new_trade_id()` with today's format `AI_YYYYMMDD_HHMMSS_xxxxxx`. The contract excludes both fields from comparisons, as R1 already does.

Mapping to today's funnel events in `decision_cycle.evaluate_symbol` (unchanged output for the default strategy):

| Decision | Events written |
| --- | --- |
| `regime_passed` False | REGIME REJECT (`regime_reason_code`, `regime_details`); TECHNICAL SKIP `TECH_SKIPPED_REGIME_REJECTED` with `details` = `regime=… regime_reason=… trend_state=…` (built in base, same text as today) |
| `regime_passed` True, `signal` None | REGIME PASS; TECHNICAL REJECT (`reason_code`, `details`) |
| `signal` set | REGIME PASS; TECHNICAL PASS (`signal.reason_code`, `details`, `trade_id`); then the existing strategic-risk, Adversarial, PM, shadow, Hard Risk and routing code, unchanged |

Replay funnel keys stay `REGIME:<regime_reason_code>` and `TECHNICAL:<reason_code>`.

### 3.4 SignalGuard and invariant ownership

`registry.build_strategy` always returns `GuardedStrategy(inner)` (`core/strategy/guard.py`). The guard runs in live and replay alike and checks every returned signal:

| Rule | Failure reason |
| --- | --- |
| `signal.symbol == strategy.symbol`; `direction` in {BUY, SELL} | `STRATEGY_SIGNAL_INVALID` |
| `stop_pips`, `take_profit_pips` finite and > 0 | `STRATEGY_SIGNAL_INVALID` |
| `risk_reward` finite and ≥ policy `MIN_RISK_REWARD`; `take_profit_pips ≥ MIN_RISK_REWARD × stop_pips − 0.02` (rounding tolerance) | `STRATEGY_SIGNAL_INVALID` |
| `order_type == "MARKET"` (the replay does not model LIMIT fills, so a LIMIT strategy would break invariant 4) | `STRATEGY_ORDER_TYPE_UNSUPPORTED` |
| `tp_mode` in {FIXED, TRAIL, HYBRID}; `be_trigger_r`, `partial_close_r`, `trailing_atr_mult` finite and ≥ 0 | `STRATEGY_SIGNAL_INVALID` |

On failure the guard returns the same decision with `signal=None`, `reason_code` as above and `details` = `field=<name> value=<v> rule=<rule>`.

Who enforces what:

| Invariant | Owner | Why there |
| --- | --- | --- |
| Min R:R 2.2 (SRS §5.3) | `SignalGuard` (strategy layer) | Today only `TechnicalAgent` enforces it; neither `validate_signal_payload` nor `HardRiskEngine.validate` (which never sees the signal) checks it. A new strategy could otherwise route a 1:1 trade. The guard is the one place every strategy passes through. Adding the same check to the Hard Risk Engine is defence in depth, but `core/risk/**` is protected, so it is backlogged for an owner-approved change (section 9). |
| Completed bars only (invariant 6) | Data sources (`PointInTimeFeed` in replay, `FileBridgeConnection` in live), plus the contract test | Strategies receive no other data handle. A static test fails if any `core/strategy/*.py` imports `MetaTrader5`, `core.mt5_bridge`, `core.file_bridge_connection`, `bridge`, or `database`, or calls `datetime.now`/`time.time` outside `new_trade_id()`. |
| Same function live and replay (invariant 4) | Registry | Both paths call `build_strategy(id, …)`. Fetch lengths ≤ 400 bars so that live (400 exported bars) and replay (`min(requested, closed)`) serve the same windows; the contract checks this with a recording fetch. |
| Hard Risk last (invariant 2) | `decision_cycle.evaluate_symbol` and `simulate` (unchanged order) | The strategy returns before Adversarial. A test with a stub strategy and a halted account shows no route. |
| Reason codes persisted (invariant 7) | `decision_cycle` funnel events | Every guard and strategy reject is a TECHNICAL or REGIME funnel event with a reason code. |
| Research-only strategies never run live or demo (owner decision 2026-11-04, H-009) | Registry flag + `build_strategy(run_context="live")` + `Engine.__init__` startup check (3.5a) | The registry is the one place every strategy is declared; the engine check runs before anything is touched, so a wrong id cannot reach the bridge. |
| Policy hash unchanged | Registry design | Strategy id and params are arguments, not policy keys (plan D, interface decisions). |

### 3.5 Registry and parameters (`fx_ai_engine/core/strategy/registry.py`)

```python
DEFAULT_STRATEGY_ID = "trend_pullback_v1"
register_strategy(strategy_id, factory, params_type, *,   # params_type: frozen dataclass or None
                  live_eligible: bool, live_block_reason: str = "")
build_strategy(strategy_id, *, symbol, fetch_ohlc, fetch_spread, policy, run_context,   # "live" | "research"
               params=None, regime_cache_key=None, on_error="raise") -> Strategy
available_strategies() -> tuple[str, ...]
is_live_eligible(strategy_id) -> bool
require_live_eligible(strategy_id) -> None                 # raises StrategyNotLiveEligibleError
canonical_params(strategy_id, params) -> dict              # defaults filled in, sorted; used in cache keys and reports
```

Each new strategy declares a frozen `Params` dataclass whose defaults are the values frozen in its hypothesis card. `params` is optional. Unknown keys, wrong types (booleans are rejected for numbers), NaN or infinite values, and values outside the declared range raise `ValueError` naming the key. `trend_pullback_v1` takes no params (it reads the policy), so a non-empty mapping is a `ValueError`. Registering an id twice, or an id that does not match `^[a-z][a-z0-9_]{2,40}$`, raises `ValueError`. A policy without `MIN_RISK_REWARD` raises `ValueError` at build time.

Strategy modules register themselves when `core/strategy/__init__.py` imports them, so live and replay see the same registry. Tests register stubs through a fixture that removes them afterwards.

### 3.5a Live eligibility (owner requirement, 2026-11-04)

The owner approved H-009 for replay and research only, until an SRS v1.x revision of the timeframe model and stop basis is approved, and asked for code that enforces this. The rule has three layers:

1. **Registry flag.** `live_eligible` is a required keyword with no default, so every strategy has to declare it. `live_eligible=False` without a non-empty `live_block_reason` raises `ValueError` at registration. Registered values:

   | Strategy | `live_eligible` | `live_block_reason` |
   | --- | --- | --- |
   | `trend_pullback_v1` | True | — |
   | `london_breakout_v1` (H-006) | True (within SRS v1) | — |
   | `h1_channel_breakout_v1` (H-007) | True (within SRS v1) | — |
   | `d1_breakout_v1` (H-009) | **False** | `requires SRS v1.x revision of timeframe model (§5.2) and stop basis (§5.3); owner decision 2026-11-04: replay/research only` |

   Being eligible does not make a strategy selectable live. Live always runs `trend_pullback_v1` (layer 3). Eligibility only says which strategies a future owner-approved switch could choose from.
2. **Build-time check.** `run_context` is a required keyword of `build_strategy`, with no default, so every caller states where the strategy will run. With `run_context="live"`, `build_strategy` calls `require_live_eligible` first and raises `StrategyNotLiveEligibleError` (a `ValueError` subclass) for an ineligible or unknown id, before calling the factory. Replay, research, diagnostics and tests pass `run_context="research"`.
3. **Engine startup check.** `main.py` defines `LIVE_STRATEGY_ID = DEFAULT_STRATEGY_ID`, with no CLI flag and no environment variable. The first statement of `Engine.__init__` is `require_live_eligible(LIVE_STRATEGY_ID)`, which runs before the bridge, the DB, the mock feedback or the agents are touched. `Engine.__init__` then builds the strategies with `run_context="live"`. `--mode demo` and `--mode smoke` both construct `Engine`, so both are covered. On failure, `Engine.__init__` logs CRITICAL `STRATEGY_NOT_LIVE_ELIGIBLE strategy=<id> reason=<live_block_reason>` and re-raises. The engine does not start, nothing is written to the bridge or the DB, and no alert is sent. This is fatal by design: the alternative, falling back to the default strategy, would hide a wrong configuration.

Replay reports for an ineligible strategy carry `"live_eligible": false` and the block reason in the report JSON and the registry row. The report markdown has a second line: `Research only: <live_block_reason>`.

Tests (`tests/test_strategy_live_eligibility.py`):
- Registering without `live_eligible` raises `TypeError`; registering `live_eligible=False` with an empty reason raises `ValueError`.
- `build_strategy("d1_breakout_v1", ..., run_context="live")` raises `StrategyNotLiveEligibleError`, and the factory is not called (spy). With `run_context="research"` it builds.
- Pinned set: the live-eligible ids are exactly `{"trend_pullback_v1", "london_breakout_v1", "h1_channel_breakout_v1"}`, and `is_live_eligible("d1_breakout_v1")` is False with a reason that mentions the SRS. Making H-009 eligible therefore needs a visible edit to this test, which the review gate on `main.py`/strategy changes will see.
- Engine refusal: monkeypatch `main.LIVE_STRATEGY_ID = "d1_breakout_v1"` and build the engine through the existing harness (`tests/engine_harness.py::make_file_bridge_engine` on a temp bridge with `repository=InMemoryEngineRepository()`). The constructor raises `StrategyNotLiveEligibleError`, the temp bridge `pending_signals/` folder stays empty, and the repository holds no rows. The same test with an unknown id also raises.
- Default pin: `main.LIVE_STRATEGY_ID == DEFAULT_STRATEGY_ID == "trend_pullback_v1"`, and `main.parse_args` has no `--strategy` option.

### 3.6 Default strategy `trend_pullback_v1` (`core/strategy/trend_pullback.py`)

`TrendPullbackStrategy(symbol, fetch_ohlc, fetch_spread, policy, *, regime_cache_key=None)` holds one `RegimeAgent` and one `TechnicalAgent`, built exactly as `main.py` lines 652–658 and `generate_candidates` build them, and calls them in the same order with the same timeframe codes. `regime_passed` = regime in {TRENDING_BULL, TRENDING_BEAR}; `regime_reason_code` = `regime.reason_code`; `regime_details` = `regime_agent.last_details`; `reason_code`/`details` = `technical_agent.last_reason_code`/`last_details`. When `regime_cache_key` is given (replay passes `lambda: feed.closed_count(symbol, "H1")`), the last `RegimeOutput` is reused while the key is unchanged. Live passes `None`, so live output, including `timestamp_utc`, is unchanged. `core/agents/**` is not edited.

### 3.7 Live wiring (`main.py`, `application/decision_cycle.py`)

`Engine.__init__` first runs the startup check in 3.5a, then builds `self.strategies = {sym: build_strategy(LIVE_STRATEGY_ID, symbol=sym, fetch_ohlc=bridge.fetch_ohlc_data, fetch_spread=bridge.get_live_spread, policy=self.policy, run_context="live", on_error="reject") for sym in SYMBOLS}`. `self.agents[sym]` keeps only `"adversarial"` (nothing else reads the other keys; checked with a grep of the worktree). There is no environment switch or CLI flag for the live strategy. Running anything other than `trend_pullback_v1` live is an owner decision with an SRS v1.x change. The existing runtime-config log line gains `strategy=trend_pullback_v1`. `decision_cycle.evaluate_symbol` replaces lines 96–149 with `decision = engine.strategies[sym].evaluate()` and the mapping in 3.3; the rest of the function reads `decision.regime` and `decision.signal` where it read `regime` and `technical`.

One deliberate behaviour change: today an exception inside `RegimeAgent` or `TechnicalAgent` propagates out of `engine_runtime.run` and stops the engine. With `on_error="reject"` the guard logs it at ERROR with the traceback (logger `fx_ai_engine.strategy`), returns a decision with `regime` = NO_TRADE, `regime_passed` False, `regime_reason_code` `STRATEGY_ERROR` and details `<ExceptionType>: <message>` (truncated to 200 characters). `decision_cycle` then writes REGIME REJECT, TECHNICAL SKIP and a risk event `STRATEGY_ERROR` at WARN. No signal is produced for that symbol on that bar (fail closed), and the other symbols and the feedback, exit and reservation handling keep running. The next M15 bar retries. This is better than stopping the loop, because a stopped loop also stops consuming exit feedback.

### 3.8 Replay and research wiring

- `backtesting/unified_replay.py`: `generate_candidates(..., strategy_id=DEFAULT_STRATEGY_ID, strategy_params=None)` builds the strategy on the `PointInTimeFeed` with `regime_cache_key`, `run_context="research"` and `on_error="raise"`. `Candidate` gains a trailing field `strategy_id: str = DEFAULT_STRATEGY_ID`, so existing positional constructors keep working. `_candidates_to_payload`/`_from_payload` carry it. `_candidate_worker`, `_candidate_cache_parts` and `generate_all_candidates` take `strategy_id` and `strategy_params`; the cache key gains `strategy_id` and `canonical_params(...)`. Trades get a `strategy_id` column.
- `backtesting/replay_cache.py`: add `core/strategy/**/*.py` to `CANDIDATE_CODE_PATHS`.
- `backtesting/run_replay.py`: `--strategy` (default `trend_pullback_v1`, choices from `available_strategies()`) and `--strategy-params` (JSON object). For a non-default strategy only `--variants baseline` is accepted (argparse error otherwise), because variants are threshold overrides of the default rules. Report JSON and registry row gain `strategy_id` and `strategy_params`. The report label is unchanged for the default and becomes `<strategy_id>__<variant>__<exit_mode>__<halts>` otherwise. `_r1_bias_audit` passes the strategy through, and its cache key gains both fields.
- `research/bias_audit.py`: `replay_decision`, `sample_decision_times`, `observe_fetch_lengths` and `run_bias_audit` take `strategy_id`/`strategy_params` (default unchanged). `observe_fetch_lengths` builds the strategy with its recording fetch instead of constructing the two agents. Without this, the R2 gate on an H-006 replay would certify the old strategy's code. Warm-up deviation (c2) keeps its EMA/ADX/RSI specs; it is non-blocking (ADR 0002 V4), and for new strategies it is reported as is. The CLI gains `--strategy`/`--strategy-params`.
- `research/common.py` (`run_replay_in_process`) and the `research.authenticity_gate` CLI: pass-through of the same two arguments, default unchanged.
- `backtesting/edge_diagnostics.py`: `--strategy`/`--strategy-params`. Default output `artifacts/replay/edge_diagnostics.json` for the default strategy, `edge_diagnostics_<strategy_id>.json` otherwise. This produces the zero-cost, RANDOM and INVERTED controls at each hypothesis's own entry times.

### 3.9 Error handling

| Operation | Failure | Recoverable? | Caller receives | Logged |
| --- | --- | --- | --- | --- |
| `build_strategy` | unknown id, bad params, missing `MIN_RISK_REWARD`, duplicate registration | Fatal | `ValueError` naming the problem. Engine init fails (engine does not start); CLIs exit 2 via argparse error | By the caller (startup error path / argparse) |
| `Engine.__init__` / `build_strategy(run_context="live")` | strategy id not live-eligible or unknown | Fatal | `StrategyNotLiveEligibleError`; engine does not start; no bridge or DB writes | CRITICAL `STRATEGY_NOT_LIVE_ELIGIBLE strategy=<id> reason=<reason>` |
| `register_strategy` | `live_eligible` missing; False without reason | Fatal (import time) | `TypeError` / `ValueError` | traceback |
| `--strategy-params` parse | not JSON, not an object | Fatal | argparse error, exit 2 | stderr |
| `evaluate()` in live | any exception | Recoverable (next bar) | `STRATEGY_ERROR` decision | ERROR with traceback; risk event WARN |
| `evaluate()` in replay/tests | any exception | Fatal for the run | exception propagates; the spawn pool is terminated (stage A code); `detached_job` writes `failed` | traceback in the job log |
| Bars missing or too few | `fetch_ohlc` empty or shorter than needed | Recoverable | strategy reject `<PFX>_DATA_UNAVAILABLE` or `<PFX>_WARMUP_INCOMPLETE` | funnel event only (as today) |
| Indicator NaN | ATR/EMA not yet defined | Recoverable | `<PFX>_INDICATORS_UNAVAILABLE` | funnel event only |
| Spread unavailable | `fetch_spread` returns None | Recoverable | new strategies reject `<PFX>_SPREAD_UNAVAILABLE` (fail closed; `TechnicalAgent` keeps its 1.5-pip fallback unchanged) | funnel event only |
| Guard rejects signal | rule in 3.4 | Recoverable | decision with `signal=None` | WARNING (live); replay counts the funnel key |
| Cache entry unreadable or meta mismatch | existing `replay_cache` behaviour | Recoverable | cache miss, regenerate | WARNING (existing) |

### 3.10 Input validation

| Input | Required | Type and limits | On failure |
| --- | --- | --- | --- |
| `strategy_id` (CLI, `build_strategy`) | optional on CLI (default), required in API | str, registered, `^[a-z][a-z0-9_]{2,40}$` | `ValueError` / argparse exit 2 |
| `run_context` (`build_strategy`) | required | `"live"` or `"research"` | other value → `ValueError`; `"live"` with ineligible id → `StrategyNotLiveEligibleError` |
| `strategy_params` | optional | JSON object; keys declared by the strategy's `Params`; int/float per field; finite; within the field's range (listed per hypothesis) | `ValueError` / exit 2 |
| Bars from `fetch_ohlc` | — | DataFrame with `open, high, low, close`, tz-aware ascending DatetimeIndex (both sources guarantee this; strategies check length and NaN only) | strategy reject reason |
| Spread | — | float ≥ 0 or None | None → reject (new strategies) |
| Signal fields | — | rules in 3.4 | guard reject |

### 3.11 Edge cases

- Regime cache: on equals off on the fixture (test). The cache key is the closed-H1 count, so a missing H1 bar cannot leave a stale regime in place for more than one H1 period.
- `Candidate` objects from an old cache cannot appear: the code fingerprint changes, so the key misses.
- Exceptions inside the guard itself (a bug in validation) take the same path as strategy exceptions.
- Fetch requests above 400 bars fail the contract test. Truncating silently would hide the live/replay difference.
- A strategy that never fires on the 4-day fixture still has to pass the contract on non-signal decisions. Each hypothesis also gets a synthetic frame on which it must fire, so the R:R and stop rules are exercised.
- Symbol priority: `simulate` processes same-time candidates in `SYMBOLS` order. With 2 open trades, `ADV_USD_STACKING` (all six pairs contain USD) and the correlation block, simultaneous signals (likely for H-006 at London open) favour EURUSD and GBPUSD. This is the same rule live uses; cards report results by symbol.

### 3.12 Testability

- Unit, no data files: registry (unknown id, duplicate, params validation, `canonical_params`), guard (one test per rule, exception paths for `raise` and `reject`), each hypothesis's rule branches on small hand-built DataFrames (a few hundred rows built in the test), static import/clock scan.
- Fixture (`tests/fixtures/live_bars_2026-11-03`, GBPUSD and AUDUSD, 400 bars per timeframe): `tests/strategy_contract.py::assert_strategy_contract(factory, frames, decision_times)` checks determinism (two runs equal without `trade_id`/`timestamp_utc`), completed bars (truncate-and-compare with `research.bias_audit.truncate_frames`), typed outputs, non-empty reasons, stop and target > 0, R:R ≥ `MIN_RISK_REWARD`, MARKET orders, and that each fetch asks for ≤ 400 bars. Run for every registered strategy.
- Integration, in-process and small: default strategy reproduces `test_replay_reproduces_live_engine_decisions_at_2026_11_03_1630` and the stage C golden decision record (`tests/fixtures/contract/decision_cycle_golden.json`) unchanged; a stub strategy flows through `generate_candidates` (different cache key), through the file-bridge engine harness (REGIME REJECT + TECHNICAL SKIP; and a signal blocked by a halted Hard Risk Engine), and through `run_replay --strategy stub --no-gate` on the tiny CSV dataset `tests/test_run_replay_gate.py` already builds (report and registry fields); R1 on the fixture with a stub strategy.
- Not tested in this wave: any replay over the real dataset (testing wave).

## 4. Shared rules for the new strategies

These apply to H-006, H-007 and H-009 and are coded once in `core/strategy/common.py`:

- **Regime:** the H1 `RegimeAgent` with the active policy (350 bars, completed only) through a `CachedRegime` helper that supports `regime_cache_key`. Each strategy states which regimes it accepts. `REGIME_DATA_UNAVAILABLE` and `REGIME_WARMUP_INCOMPLETE` always reject.
- **Clock:** last closed M15 bar open + 15 min; London and New York clocks via `zoneinfo`.
- **Orders:** MARKET, filled at the next M15 open in replay (as today).
- **Exits:** fixed bracket: `tp_mode="FIXED"`, `be_trigger_r=0`, `partial_close_r=0`, `trailing_atr_mult=0`. The EA skips break-even and partial close when the value is 0 (`FX_Execution.mq5` lines 593 and 636) and only trails in TRAIL/HYBRID mode (line 660), so no EA change is needed. Reason: E5 (break-even at 0.8R holds the average winner at 1.0R; ADR 0001 D2 asks for 1.5R).
- **Target:** the same spread-adjusted formula as `TechnicalAgent` (lines 322–332): `tp = max(stop × min_rr, min_rr × (stop + s/2) + s/2)` with `s` the current spread in pips and `min_rr` = policy `MIN_RISK_REWARD` (2.2). No other target.
- **Stops:** structure-based on M15 bars (SRS §5.3), except H-009. Each strategy rejects stops outside its range (`<PFX>_STOP_TOO_TIGHT` / `_STOP_TOO_WIDE`); stops are never padded out to a minimum. Reason: E3/E4.
- **Other signal fields:** `confidence=0.5` (not used for gating or sizing anywhere today; checked), `rsi_at_entry`, `rsi_slope` and `spread_entry` filled the same way `TechnicalAgent` fills them, so the ranker and meta-labeler features stay defined.
- **Fetch lengths:** ≤ 400 bars per call.
- **Not changed:** risk per trade 3.2%, max 2 open trades, 5% exposure, the daily, weekly, drawdown and loss-streak halts, the Adversarial gates (including `ADV_RSI_EXHAUSTION_*`, which will reject some breakout entries; that is the system as it would run live), and PortfolioManager sizing.

## 5. Hypotheses (owner-approved 2026-11-04)

### 5.1 H-006 `london_breakout_v1`: London-open breakout of the Asian range (within SRS v1)

Rules (frozen):
- Asian range: high and low of the M15 bars whose open time falls in [00:00, 07:00) Europe/London on the decision's London date. All 28 bars must be present, else `LBO_RANGE_INCOMPLETE` (holidays, data gaps).
- Entry window: decision time in (07:00, 10:00] London time (inside the existing London session).
- Trigger: last M15 close above the range high (BUY) or below the range low (SELL), and no earlier M15 close since 07:00 London that day closed outside the range on either side. This gives one attempt per symbol per day and needs no stored state, so truncation tests stay exact. Otherwise `LBO_NO_BREAKOUT` or `LBO_ALREADY_BROKEN`.
- Regime: reject if the H1 regime is trending against the breakout (`LBO_AGAINST_H1_TREND`); any other regime is accepted.
- Stop: opposite side of the Asian range ± 1 pip; accepted range 20–60 pips.
- Params (ranges for validation): `range_start_hour=0` [0, 6], `range_end_hour=7` [1, 8], `entry_end_hour=10` [8, 12], `min_stop_pips=20` [5, 100], `max_stop_pips=60` [10, 200], `stop_buffer_pips=1` [0, 5].

Rationale: E7 shows the current entries fire on a third of trending bars with no event behind them, and E1 shows that timing carries no information. This hypothesis ties the entry to one dated liquidity event per day, the London open, when European order flow arrives after the quiet Asian session. The stop sits at a structural level that is usually 20–40 pips away, so costs fall from about 0.31R (E3) to about 0.08–0.12R (E4 arithmetic). The fixed 2.2R bracket keeps the full winner (E5). This is a practitioner idea; I am not citing academic evidence for it. Known gap: the live engine applies the news blackout and the replay does not (`run_replay.ASSUMPTIONS`). UK and euro-area releases cluster in this window, so live would skip some trades the replay takes.

### 5.2 H-007 `h1_channel_breakout_v1`: two-day channel breakout inside the current trend regime (within SRS v1)

Rules (frozen):
- Regime: TRENDING_BULL allows BUY only, TRENDING_BEAR allows SELL only. This is the baseline's own regime gate, unchanged (`HCB_REGIME_NOT_TRENDING`).
- Channel: highest high and lowest low of the last 48 closed H1 bars.
- Trigger: last M15 close beyond the channel in the trend direction, with the previous M15 close inside it (first close through), else `HCB_NO_BREAKOUT`.
- Stop: lowest low (BUY) or highest high (SELL) of the last 16 closed M15 bars ∓ 1 pip; accepted range 20–60 pips.
- Params: `channel_h1_bars=48` [12, 240], `stop_lookback_m15=16` [4, 96], `min_stop_pips=20`, `max_stop_pips=60`, `stop_buffer_pips=1`.

Rationale: this keeps the baseline's regime filter and changes only the entry event, the stop and the exit. It is the cleanest comparison against `trend_pullback_v1`. If the H1 trend label has value, a new two-day extreme in its direction should follow through more often than the pullback entries, of which only 9.6% reached 2.2R (E5). Wider structural stops cut costs (E4). If H-007 fails while the regime is unchanged, that points at the regime layer itself having no value for direction, which is useful to know for every later design.

### 5.3 H-009 `d1_breakout_v1`: 20-day breakout trend following (replay/research only until an SRS revision is approved)

Rules (frozen):
- Direction: the last closed D1 bar closes above the highest high (BUY) or below the lowest low (SELL) of the 20 D1 bars before it.
- Entry: the first session decision (`is_tradeable_session`) at or after that D1 bar's close, computed from the M15 bar times between the D1 close and now. Otherwise `D1T_NOT_FIRST_SESSION_DECISION` or `D1T_NO_BREAKOUT`.
- Regime: reject if the H1 regime is trending against the direction (`D1T_AGAINST_H1_TREND`).
- Stop: 2.0 × ATR(20) on D1; accepted range 40–300 pips.
- Params: `channel_d1_bars=20` [5, 100], `atr_d1_period=20` [5, 50], `stop_atr_mult=2.0` [0.5, 5.0], `min_stop_pips=40`, `max_stop_pips=300`.

Rationale: the strongest form of the cost lesson. At 100–200-pip stops, round-trip costs are about 0.01–0.03R (E3/E4). Time-series momentum in currency futures has published support (Moskowitz, Ooi and Pedersen, "Time series momentum", Journal of Financial Economics, 2012), though the effect in G10 currencies is weaker in recent decades. SRS conflict: direction comes from D1 and the stop from D1 ATR. That changes the timeframe model (SRS §5.2, H1 regime and M15 execution; listed in §15 change control) and the stop basis (§5.3, M15). It can be coded and replayed in development and testing, because the replay is research. The owner accepted in principle (2026-11-04) that an SRS v1.x revision of the timeframe model and stop basis may follow. Until that revision is approved, H-009 is replay/research only: the registry marks it `live_eligible=False` and the engine refuses to start with it (section 3.5a). Positions will be held for days to weeks, so swap matters (section 6, step 5), and positions will often occupy both open-trade slots for long periods. Trade count is the main risk: 20-day breakouts are rare, so the ≥ 100 development trades rule may make the card inconclusive.

### 5.4 Dropped by the owner: H-008 `range_fade_v1`

Proposed in revision 1: fade a failed probe of the 24-hour M15 range when the H1 trend state is FLAT, with a 15–40-pip stop beyond the probe bar and a fixed 2.2R target. Its basis was the INVERTED control (+0.052R at zero cost, CI +0.004 to +0.100, E2). The design flagged it as the weakest case: that gross effect is a sixth of the cost at today's stops, and the locked 2.2 minimum R:R works against range trades. **Dropped by the owner on 2026-11-04.** No module, tests or card are written for it. ID H-008 stays unused so that card numbers keep matching this design.

### 5.5 Considered and not proposed

- Hour-of-day filter on the current strategy: E9 shows the development pattern reversing in the holdout.
- Trading the current strategy inverted: it is mined from a control row, and its +0.05R gross is a sixth of the 0.31R cost.
- Same entries with wider stops: E4 shows >20-pip stops still lose 0.17R (−0.14R before commission).
- ML meta-labeling of the current entries: the AI layer stays shadow-only in v1 (ADR 0001 D6, stage E), and filtering entries that carry no information rarely creates an edge.
- LIMIT-order pullback entries: `simulate` does not model LIMIT fills, so live and replay would differ (invariant 4). It would need replay fill modelling first.
- Lower minimum R:R for mean reversion: locked constant; needs owner approval and an SRS revision.

## 6. Pre-registered evaluation plan (common to all cards, for the testing wave)

1. **Freeze.** The card, with its parameters, is committed before the run. The run uses the card's defaults and no `--strategy-params`; any parameter change means a new card. Dataset `artifacts/datasets/mt5_2026-11-03` (manifest hashes in the result). Development 2022-12-24 to 2026-05-03 17:00 UTC; holdout = last 6 months (`run_replay` default). The holdout was read for the current strategy in Phase 2, but none of these hypotheses was designed on it; the only holdout number used in this design is E9, used to reject an idea. If a newer export extends past 2026-11-03, the extension is a second, untouched holdout, read once.
2. **Runs** (from `fx_ai_engine/`, each through `python -m validation.detached_job start --name <card> -- …`, 2 workers):
   - Arm: `python -m backtesting.run_replay --data-dir artifacts/datasets/mt5_2026-11-03 --strategy <id> --variants baseline --exit-modes policy --costs base stress_1_5x stress_2x --halts edge`
   - Controls: `python -m backtesting.edge_diagnostics --data-dir artifacts/datasets/mt5_2026-11-03 --strategy <id>`
   - Baseline (once for the family): `python -m backtesting.run_replay --data-dir artifacts/datasets/mt5_2026-11-03 --variants baseline --exit-modes policy fixed_tp --costs base stress_1_5x --halts edge`
   - SRS view (information only): the arm with `--halts srs --costs base` (drawdown-gate reachability at 3.2%; R5 Monte Carlo at 1.0–3.2%).
3. **Gate.** The arm's R2 verdict must be PASS. R2 includes `R4_COST_STRESS` (expectancy at 2× costs > 0) as a blocking check, so the losing baseline can never PASS. For the baseline and control rows, require only the authenticity checks (R1A, R1B, R1C parity, and the three fill checks) to pass. This also affects H-001 to H-005: under README rule 3 they can never produce a result while the baseline loses. That should be fixed in the testing wave's README update (section 9).
4. **Decision on the development section at `stress_1_5x`, after the swap allowance (step 5).** "Not rejected" requires all of:
   - ≥ 100 development trades (else `inconclusive`);
   - expectancy ≥ +0.25R, profit factor ≥ 1.3, average winner ≥ 1.5R (ADR 0001 D2);
   - lower confidence bound > 0, computed as mean − z·s/√n with z = 2.39 (Bonferroni over the approved family of k = 3 cards, H-006, H-007 and H-009: two-sided 0.05 / 3; owner decision 2026-11-04, written into each card);
   - direction information: the zero-cost expectancy minus the RANDOM-direction zero-cost expectancy is greater than 2·√(s₁²/n₁ + s₂²/n₂), and the zero-cost expectancy exceeds INVERTED's;
   - stability: expectancy > 0 in at least 60% of development half-year windows with ≥ 20 trades;
   - concentration: expectancy stays > 0 after removing the symbol with the highest net R.
   It is `rejected` if expectancy ≤ 0 at `stress_1_5x`, or if any rule above fails with ≥ 100 trades. The baseline is reported side by side but is not a pass condition (it is −0.35R).
5. **Swap allowance** (the replay has no swap, `run_replay.ASSUMPTIONS`). For each trade, count the 17:00 America/New_York rollovers in (entry, exit], with Wednesday counting 3. Charge only negative swap: `R −= rollovers × max(0, −swap_side) × point / pip / stop_pips`, using the per-symbol `swap_long`/`swap_short` from the bridge `market/symbols.json` snapshot taken at test time (file hash recorded). The 2026-11-04 snapshot, in points: EURUSD −0.7/−1.0, GBPUSD −0.2/−2.2, USDJPY −0.1/−0.6, AUDUSD +6.3/−14.8, USDCAD −0.8/−0.4, USDCHF +0.1/−1.0. The EA does not export `swap_mode`. Confirm "points" with a read-only MT5 MCP symbol query; if that is not possible, charge 1.0 pip per rollover. Implemented as `research/swap_allowance.py` (section 8).
6. **Holdout,** read once, after the development decision is written into the card: it confirms the result if expectancy at `stress_1_5x` (swap-adjusted) is > 0 with ≥ 30 trades; otherwise the holdout result is `inconclusive` or `contradicts`. Nothing is re-tuned afterwards.
7. **Also reported:** SRS §12.2 metrics (win rate, average R) per ADR 0001 D2, by-symbol and by-hour tables, Adversarial and PM rejection counts, median holding time, and the number of registry rows in the family (multiple-testing ledger).
8. **Promotion:** a "not rejected" card with a confirming holdout does not change the live strategy by itself. Moving a strategy to the demo is an owner decision, together with SRS v1.1 (Phase 3), including the risk level (ADR 0001 D3).

## 7. Draft hypothesis cards

These go to `fx_ai_engine/docs/research/hypotheses/` as written below. The owner approved H-006, H-007 and H-009 on 2026-11-04 with family size k = 3; H-008 was dropped and gets no card. The template follows the README's fields; for new strategies, "Parameter / active / coded default" becomes "Strategy / comparator", and "Owner's original motivation" becomes "Proposed by / approved". README rules 1–5 apply unchanged.

### H-006-london-breakout.md

```markdown
# H-006: London-open breakout of the Asian range vs trend_pullback_v1
- ID: H-006
- Strategy: `london_breakout_v1`, `fx_ai_engine/core/strategy/london_breakout.py`
- Frozen parameters: range 00:00–07:00 Europe/London (28 M15 bars required); entry decisions (07:00, 10:00] London; first close outside the range only; stop at opposite range side ± 1 pip, accepted 20–60 pips; fixed 2.2R spread-adjusted target; no break-even, partial or trail; reject if the H1 regime trends against the breakout
- Comparators: `trend_pullback_v1` baseline (active core_srs policy, policy and fixed_tp exits); RANDOM and INVERTED direction controls at H-006 entry times (`edge_diagnostics --strategy london_breakout_v1`)
- SRS fit: within SRS v1 (H1 regime, M15 execution, M15 structure stop, R:R ≥ 2.2)
- Proposed by / approved: stage D design 2026-11-04 / owner, 2026-11-04
- Family size k: 3 (H-006, H-007, H-009), Bonferroni z = 2.39
- Status: pre-registered, not run
- Date written: <commit date>
## Mechanism
The Asian session range marks where overnight liquidity cleared. European order flow at the London open either holds price inside it or pushes through. A close outside the range on the first attempt is the event; the stop sits on the far side of the range, a structural level 20–60 pips away, so the round-trip cost is about 0.04–0.12R instead of the baseline's 0.31R (Phase 2 E3, E4). A fixed 2.2R bracket keeps the whole winner (E5: the live exits cap the average winner at 1.0R).
## Prediction
At zero cost the breakout direction beats its RANDOM-direction control at the same entry times; after 1.5× costs and the swap allowance, development expectancy is ≥ +0.25R with profit factor ≥ 1.3 and average winner ≥ 1.5R.
## Evaluation commands
From `fx_ai_engine/`, each via `python -m validation.detached_job start --name h006 -- …`:
python -m backtesting.run_replay --data-dir artifacts/datasets/mt5_2026-11-03 --strategy london_breakout_v1 --variants baseline --exit-modes policy --costs base stress_1_5x stress_2x --halts edge
python -m backtesting.edge_diagnostics --data-dir artifacts/datasets/mt5_2026-11-03 --strategy london_breakout_v1
python -m backtesting.run_replay --data-dir artifacts/datasets/mt5_2026-11-03 --strategy london_breakout_v1 --variants baseline --exit-modes policy --costs base --halts srs
python -m research.swap_allowance --trades <arm stress_1_5x _trades.csv> --symbols-json <snapshot> --holdout-start <report holdout_start>
## Pre-registered rejection rule
Development section, `stress_1_5x`, swap-adjusted. Inconclusive if < 100 development trades or the arm's R2 verdict is not PASS. Not rejected only if all hold: expectancy ≥ +0.25R; PF ≥ 1.3; avg winner ≥ 1.5R; mean − 2.39·s/√n > 0 (family size k = 3); zero-cost expectancy − RANDOM zero-cost expectancy > 2·√(s₁²/n₁ + s₂²/n₂) and zero-cost expectancy > INVERTED's; expectancy > 0 in ≥ 60% of half-year windows with ≥ 20 trades; expectancy > 0 after removing the best symbol. Otherwise rejected. Holdout read once afterwards: confirms if expectancy at `stress_1_5x` > 0 with ≥ 30 trades.
Known limitation: the replay has no news blackout; live would skip some London-open trades.
## Result
(empty until the arm passes the R2 gate)
```

### H-007-h1-channel-breakout.md

```markdown
# H-007: Two-day H1 channel breakout in the trend regime vs trend_pullback_v1
- ID: H-007
- Strategy: `h1_channel_breakout_v1`, `fx_ai_engine/core/strategy/h1_channel_breakout.py`
- Frozen parameters: regime TRENDING_BULL → BUY only, TRENDING_BEAR → SELL only (active RegimeAgent thresholds); channel = high/low of the last 48 closed H1 bars; trigger = first M15 close through the channel in the trend direction; stop = 16-bar M15 swing ∓ 1 pip, accepted 20–60 pips; fixed 2.2R spread-adjusted target; no break-even, partial or trail
- Comparators: `trend_pullback_v1` baseline (same regime gate); RANDOM and INVERTED controls at H-007 entry times
- SRS fit: within SRS v1
- Proposed by / approved: stage D design 2026-11-04 / owner, 2026-11-04
- Family size k: 3 (H-006, H-007, H-009), Bonferroni z = 2.39
- Status: pre-registered, not run
- Date written: <commit date>
## Mechanism
Same H1 trend label as the baseline, different entry event: a close through a two-day extreme in the trend direction instead of any bar near EMA50 (Phase 2 E7: the baseline fires on 33% of trending session bars). Only 9.6% of baseline entries reached 2.2R (E5); a breakout of a multi-day extreme should follow through more often if the trend label has value. The 16-bar swing stop puts costs at about 0.04–0.12R (E4).
## Prediction
Zero-cost expectancy beats RANDOM at the same entry times; after 1.5× costs and the swap allowance, development expectancy ≥ +0.25R, PF ≥ 1.3, average winner ≥ 1.5R. If it fails, the H1 trend label itself carries no usable direction at these horizons.
## Evaluation commands
As H-006 with `--strategy h1_channel_breakout_v1` and job name `h007`.
## Pre-registered rejection rule
As H-006 (same thresholds, same family correction, same holdout rule).
## Result
(empty until the arm passes the R2 gate)
```

### H-009-d1-breakout.md

```markdown
# H-009: 20-day breakout trend following vs trend_pullback_v1
- ID: H-009
- Strategy: `d1_breakout_v1`, `fx_ai_engine/core/strategy/d1_breakout.py`
- Frozen parameters: BUY when the last closed D1 bar closes above the highest high of the previous 20 D1 bars (SELL mirrored); entry at the first session decision after that D1 close; reject if the H1 regime trends against; stop = 2.0 × ATR(20, D1), accepted 40–300 pips; fixed 2.2R spread-adjusted target; no break-even, partial or trail
- Comparators: `trend_pullback_v1` baseline; RANDOM and INVERTED controls at H-009 entry times
- SRS fit: replay/research only. D1 direction and D1 ATR stop change the timeframe model (SRS §5.2, §15) and the stop basis (§5.3). The owner accepted in principle (2026-11-04) that an SRS v1.x revision may follow; until it is approved the strategy is registered `live_eligible=False` and `Engine` refuses to start with it (`tests/test_strategy_live_eligibility.py`). A "not rejected" result does not change this
- Proposed by / approved: stage D design 2026-11-04 / owner, 2026-11-04
- Family size k: 3 (H-006, H-007, H-009), Bonferroni z = 2.39
- Status: pre-registered, not run
- Date written: <commit date>
## Mechanism
Time-series momentum: currencies that make a new 20-day extreme tend to continue (Moskowitz, Ooi and Pedersen, JFE 2012; weaker in G10 FX in recent decades). With 100–200-pip stops the round-trip cost is about 0.01–0.03R (Phase 2 E3, E4), so even a small directional edge survives costs. Holding periods of days to weeks make swap material; the swap allowance applies.
## Prediction
Zero-cost expectancy beats RANDOM; after 1.5× costs and the swap allowance, development expectancy ≥ +0.25R, PF ≥ 1.3, average winner ≥ 1.5R. Main risk: fewer than 100 development trades (inconclusive).
## Evaluation commands
As H-006 with `--strategy d1_breakout_v1` and job name `h009`. Also report median holding days and the share of time both open-trade slots are occupied.
## Pre-registered rejection rule
As H-006.
## Result
(empty until the arm passes the R2 gate)
```

## 8. Implementation sequence (after approval) and acceptance checks

Follows plan section D (D1–D4) with the refinements above. Tests are committed before the switch-over, as in stage C.

| Step | Work | Files | Verify |
| --- | --- | --- | --- |
| D1 | Package: types, guard, registry (with `live_eligible`, `run_context`, `StrategyNotLiveEligibleError`), `trend_pullback_v1`, `common.py` helpers; cache code paths | `core/strategy/{__init__,base,guard,registry,common,trend_pullback}.py`, `backtesting/replay_cache.py` | `tests/test_strategy_interface.py` unit part |
| D2 | Contract helper and interface tests (before switching callers) | `tests/strategy_contract.py`, `tests/test_strategy_interface.py` | contract passes for the default strategy on the fixture |
| D3 | Live and replay use the strategy object; `main.LIVE_STRATEGY_ID` and the `Engine.__init__` startup check (3.5a); research and diagnostics pass-through; CLI flags | `main.py`, `application/decision_cycle.py`, `backtesting/{unified_replay,run_replay,edge_diagnostics}.py`, `research/{bias_audit,common,authenticity_gate}.py` | golden decision record and live-parity test unchanged; research tests; full suite |
| D4 | ADR 0004 (strategy id outside the policy hash, live default fixed, live eligibility and the H-009 research-only rule, guard owns min R:R, replay-only regime cache, live exception handling) and STATUS | `docs/decisions/0004-strategy-plugin-interface.md`, `docs/STATUS.md` | — |
| D5–D7 | One per approved hypothesis (D5 H-006 `london_breakout`, D6 H-007 `h1_channel_breakout`, D7 H-009 `d1_breakout` registered `live_eligible=False`): module + unit tests on synthetic frames + `assert_strategy_contract` on the fixture + card + README table row. D7 also adds `tests/test_strategy_live_eligibility.py` (3.5a); its registry and build-time tests can land in D1, the pinned-set and engine-refusal tests land with D7 | `core/strategy/<module>.py`, `tests/test_strategy_<id>.py`, `tests/test_strategy_live_eligibility.py`, `docs/research/hypotheses/H-00N-*.md`, `.../README.md` | `pytest tests/test_strategy_<id>.py tests/test_strategy_live_eligibility.py` |
| D8 | `research/swap_allowance.py` (rollover count, negative-swap charge, dev/holdout split; CLI) with unit tests on a 5-row CSV; README rule 3 note on the baseline gate (section 6 step 3); one line in `.kiro/skills/fx-hypothesis-cards/SKILL.md` that cards may also cover strategies | as listed | `pytest tests/test_research_swap_allowance.py` |

Stage acceptance: full suite green with output (`USE_MT5_MOCK=1 nice -n 10 /Users/raynj/Desktop/Forex-automation/.venv/bin/python -m pytest -q -p no:cacheprovider` from the worktree's `fx_ai_engine/`); `python -m validation.print_policy_hash --mode core_srs` identical on `d760534` and on the stage D head; `tests/test_srs_locked_constants.py` green; no replays over the real dataset; stray-process check clean. Review: `main.py` and `application/decision_cycle.py` are guardrail paths and need a different-model review and owner approval before merge. No protected path is touched.

## 9. Backlog and open points

- **Min R:R in the Hard Risk Engine** (defence in depth): `HardRiskEngine` would need the signal's R:R; `core/risk/**` is protected. Proposed for the owner and for the SRS v1.1 proposal (stage F).
- **News blackout in replay:** live applies `is_news_blackout`, the replay does not (documented assumption). This matters most for H-006. It is a separate change to `generate_candidates`, and it needs historical calendar data, which the dataset does not have.
- **Strategy id in persisted live records:** only the default can run live, so the id is implied by the code version and the startup log. Add a DB column when a second live strategy is allowed (owner/SRS decision).
- **R2 gate and comparison cards:** under README rule 3, H-001 to H-005 can never reach a result while the baseline fails R4. This design resolves it for H-006, H-007 and H-009 (section 6 step 3); the testing wave should apply the same rule to H-001 to H-005 through a README change, recorded in the cards' history.
- **Swap mode:** confirm `SYMBOL_SWAP_MODE` with a read-only MCP query before using the snapshot rates.

## 10. Owner decision

Decision received 2026-11-04 (relayed by the parent session), recorded verbatim in substance:

- **Approved:** H-006 `london_breakout_v1`, H-007 `h1_channel_breakout_v1`, H-009 `d1_breakout_v1`.
- **Dropped:** H-008 `range_fade_v1`. Not implemented, no card; listed in section 5.4 as considered and dropped by the owner.
- **Family size:** k = 3, Bonferroni z = 2.39. Written into the evaluation plan (section 6 step 4) and into each card, with approval date 2026-11-04.
- **H-009:** the owner accepts in principle that an SRS v1.x revision of the timeframe model and stop basis may follow. Until that revision is approved, H-009 is replay/research only. It must not be selectable for live or demo runs, enforced in code with a test. Implemented in this design as the registry flag `live_eligible=False`, the `build_strategy(run_context="live")` check and the `Engine.__init__` startup refusal, with `tests/test_strategy_live_eligibility.py` (section 3.5a).
- **Live default:** `trend_pullback_v1`, unchanged; no CLI or environment switch.
- **Everything else:** stands as written in revision 1.

Approved hypothesis list (final):

| Card | Strategy | SRS fit | Live eligible |
| --- | --- | --- | --- |
| H-006 | `london_breakout_v1` | within SRS v1 | yes (not selected; live runs `trend_pullback_v1`) |
| H-007 | `h1_channel_breakout_v1` | within SRS v1 | yes (not selected; live runs `trend_pullback_v1`) |
| H-009 | `d1_breakout_v1` | needs SRS v1.x revision (§5.2, §5.3) | **no**, replay/research only, code-enforced |

Nothing is open. Implementation (D1–D8) can start.

### Changes in revision 2

- Added section 3.5a (live eligibility) and the matching rows in 3.4, 3.9 and 3.10; `run_context` added to `build_strategy`; `LIVE_STRATEGY_ID` and the startup check added to 3.7.
- Removed H-008 from the hypotheses and cards; added 5.4 "Dropped by the owner"; removed the revision 1 recommendation.
- Fixed k = 3, z = 2.39 in section 6 and the cards; filled in the approval date.
- Renumbered the implementation steps to D1–D8.
