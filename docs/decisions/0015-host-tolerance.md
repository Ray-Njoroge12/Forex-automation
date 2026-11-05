# ADR 0015: Host tolerance on the Mac (host guard, missed cycles, outage alert, EA ATR cache, gate H1)

Date: 2026-11-05. Status: accepted for development; the EA change (protected path) needs a review by a different model and owner approval before merge.

## Context

The owner's 8 GB MacBook sleeps, runs on battery, throttles MT5 (App Nap) and runs replays next to the engine (`fx_ai_engine/docs/2026-11-04-mac-host-operating-model.md`, STATUS issues 20 and 21). Section 6 of that doc lists five backlog items; section 7 defines gate H1.

## Decisions

1. **One rule set.** `core/host_rules.py` holds the parsers, budgets and rule functions. `validation/host_check.py` (CLI, unchanged output) and the engine use it. The engine evaluates only `guard_checks`: power, sleep prevention, App Nap, replay/research processes, bridge snapshot age (30 s) and swap. It runs about nine short commands and never samples MT5 CPU or calls `footprint`.
2. **Host guard is an entry block, not a risk limit.** `application/host_tolerance.HostGuard` runs at engine start, every 300 s, every 30 s while blocked, and at the first poll inside each UTC day's trading window. A BLOCK on one of the five rules (or a probe error, fail-closed) stops the decision cycle before any strategy stage: funnel stage `HOST`, reason `HOST_GUARD_BLOCK` per symbol, and risk event `HOST_GUARD_BLOCK` (BLOCK) on entry into the blocked state or when the failing set changes. `HOST_GUARD_RECOVERED` (INFO) is recorded when it passes again. Feedback, exits, closure recovery and account updates still run every loop. Router housekeeping (stale-signal quarantine) still runs before the check. Locked constants, the Hard Risk Engine and its position at the end of the flow are unchanged. Swap is recorded but never blocks.
3. **Where the guard applies.** Only `--mode demo|live`, not mock, and only on macOS (`sys.platform == "darwin"`); elsewhere it is disabled and says why in the log. Tests get a disabled guard from `tests/conftest.py` and inject their own probes.
4. **Daily host record for H1.** The first evaluation inside the window each UTC day is persisted as `HOST_GUARD_DAILY_CHECK` (INFO) with `status=`, `failed=`, `swap_used_mb=` and every check's status.
5. **Missed cycles.** Expected cycles are the weekday M15 slots whose start is inside a session (`core/filters/session_filter.py`). Evaluated cycles are slots with a `SESSION`/`PASS` funnel row. Only `execution_mode='mt5'` rows count (`validation/cycle_counter.py`; reported by `burnin_status`). Holidays are not modelled.
6. **Outage alert.** `OutageMonitor` sends one message through `core.alerts` when the account snapshot or the bar exports (status heartbeat, or the M15 bar more than one bar late) are stale for more than 120 s inside trading hours, and one when they recover. It does nothing when `core.alerts.alerts_configured()` is false and is off in mock mode. A failed delivery is logged and not retried, so an outage never sends more than two messages.
7. **EA ATR cache (protected).** `GetATR` reuses one `iATR` handle per symbol/timeframe/period (cache of 16, reset if full) and `OnDeinit` releases them. Order placement and the envelope checks are unchanged. EA build `2026.11.05-1`. A side effect: MT5 calculates a new indicator handle asynchronously, so the old code could read a handle before it had calculated. In that case `GetATR` returned 0 and the trail skipped that tick. A cached handle has calculated by the next tick, so the trail may now move on ticks where it used to skip. The trail rules are the same.
8. **Gate H1 evaluator.** `python -m validation.gate_h1` (read-only) checks, per completed trading day: the daily host record within 15 minutes of the window opening with PASS, swap below 1500 MB in it, at most 6 MT5 connection drops in the window (terminal logs, local time), no Sleep/DarkWake in `pmset -g log` during the window, and zero missed cycles. Missing data is NO_DATA and does not pass. H1 passes on 5 or more consecutive completed trading days, counted back from the latest.

## Consequences

- A host problem during trading hours now appears as a reason code, a missed-cycle number and (with Telegram configured) two messages. Before, it showed only as silence.
- H1 cannot be met until the engine runs in Mode T with this build, because the daily host record is new.
- Not measured from recorded data: memory and disk at the window start, MT5 settings, and the physical setup (UPS, backup link).
