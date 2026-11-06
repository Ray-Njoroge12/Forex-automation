# Running the system on this Mac: limits, causes and operating route

Date: 2026-11-04 (section 6 backlog built on 2026-11-05, ADR 0015). Host: MacBook Pro M2 (Mac14,7), 8 GB RAM, 8 cores, macOS 27.2, MT5 build 6296 under MetaQuotes' Wine (x86-64 translated by Rosetta). Every number below was measured on this machine today unless marked otherwise. Tool: `python -m validation.host_check` (read-only).

## 1. Answer

The Mac can host the trading system, including a small live account later, if it runs as a dedicated trading machine during trading hours and as a development machine outside them. The system itself is light. The engine needs about 0.15 GB and a few seconds of CPU every 15 minutes. The failures seen on 2026-11-03/04 came from three host problems, and all three can be fixed on this Mac:

1. **The Mac was sleeping.** System sleep is set to 1 minute. Once the keep-awake agent was uninstalled, the Mac cycled through sleep and DarkWake 172 times on 2026-11-04. The MT5 connection drops (98 in two days) sit almost entirely in those hours.
2. **Memory is oversubscribed.** Kiro (two windows plus agent sessions) uses about 6.2 GB, browsers 1.6 GB and MT5 1.6 GB, on an 8 GB machine. Swap is at 4.9 of 6 GB.
3. **MT5 is configured heavier than needed.** "Max bars in chart" is Unlimited (set for the history export), News is on, and the terminal runs translated x86 code. It uses about 100% CPU continuously.

The rest of this document gives the evidence, the operating route (three modes and a daily schedule), the design rules that make the system tolerate a laptop host, and what still has to be true before real money.

## 2. Measurements (2026-11-04, about 16:30 UTC)

| Item | Value | Effect |
| --- | --- | --- |
| RAM / swap | 8 GB; swap 4.9 GB used of 6 GB; memory free 34–42% | Constant paging; MT5 and the engine are swapped out and slowed |
| Kiro footprint | ≈ 6.2 GB across all Kiro processes (two windows, several agent sessions and workflows; single renderers 450–680 MB) | Largest consumer by far |
| Browsers (Brave) | ≈ 1.6 GB | Second-largest optional consumer |
| MT5 terminal | 1.64 GB footprint; CPU 87–118% (5 samples over 25 s) | Fixed cost of trading; can be reduced (section 4.2) |
| Engine (when running) | ≈ 80–150 MB; bursts of < 1 s per M15 cycle | Negligible |
| Replays | 6 spawn workers on 2026-11-03 (now capped at 2, niced, detached) | Saturated the CPU; must not overlap trading hours |
| Power | On battery at the time of measuring (100%, 4 h 53 m remaining) | macOS blocks system sleep only on AC power |
| Sleep settings (AC) | `sleep 1`, `displaysleep 10`, `powernap 1`, `standby 1` | Sleeps after 1 minute idle unless something holds an assertion |
| Sleep events | 172 DarkWakes on 2026-11-04 so far; sleep/wake every ~40 s at 08:40 local | MT5 loses its connection, timers stop, snapshots go stale |
| MT5 connection drops | 21 on 11-03, 77 on 11-04; 11-04 hourly: 7, 12, 9, 10, 7, 9, 9, 4, 9 from 00:00 to 08:00 local; 1 after 09:00 | Drops follow the sleep hours, not the market or the server |
| MT5 App Nap | Not disabled for `net.metaquotes.wine.MetaTrader5` | macOS can throttle MT5's timers when its window is hidden |
| MT5 settings | `MaxBars=100000000` (Unlimited), `NewsEnable=1`, `PreloadCharts=1`, one chart open | Extra memory and CPU for nothing the system uses |
| Disk | 13 GB free of 228 GB | Enough; BLOCK below 5 GB |

## 3. What the system actually needs

| Component | Needs | When |
| --- | --- | --- |
| MT5 terminal + EA | ≈ 1.0–1.3 GB after the settings in 4.2, one core at most | Whenever a position is open, and during trading hours |
| Engine (`main.py --mode demo`) | ≈ 0.15 GB, < 1 s CPU per M15 bar | Trading hours (decisions only happen in the London/NY sessions) |
| Watchdog, keep-awake, calendar refresh | < 50 MB | With the engine |
| macOS itself (WindowServer, kernel, services) | ≈ 2.5–3 GB | Always |
| **Trading total** | **≈ 4–4.5 GB of 8 GB** | Leaves 3.5 GB headroom |
| Kiro, agents, workflows, browser | 3–8 GB | Development mode only |
| Replay (2 workers) | 1–2 GB, 2 cores for minutes to an hour | Research mode only |

Conclusion: trading and development each fit on this Mac, but not at the same time. Running both at once is what pushed swap to 4.9 GB.

## 4. Causes and fixes

### 4.1 Sleep and the connection drops

Evidence: `pmset -g custom` shows `sleep 1` on AC and battery. `pmset -g log` shows the Mac alternating Sleep, DarkWake and Wake from Deep Idle every 35–40 s overnight. MT5 drops cluster in the same hours (section 2). The earlier STATUS note "cause unknown" (issue 21) is answered: it is mostly host sleep.

Fixes:
- Keep-awake agent (`com.fxai.keepawake`, `caffeinate -i -m -s`) during trading mode. `-s` only works on AC power, so the charger is mandatory.
- Disable App Nap for MT5: `defaults write net.metaquotes.wine.MetaTrader5 NSAppSleepDisabled -bool YES` (takes effect after MT5 restarts). Without it macOS may throttle the EA's 1-second timer while the window is hidden or the display is off.
- Optional (administrator), on AC only: `sudo pmset -c sleep 0 powernap 0 standby 0 disksleep 0`. This stops Power Nap and standby churn even if the agent is not loaded. Undo: `sudo pmset -c sleep 1 powernap 1 standby 1 disksleep 10`.
- Lid: keep it open. A closed lid sleeps the Mac unless an external display, keyboard and power are attached (clamshell mode). The display may sleep; the system will not.
- Re-measure drops after these changes. If they stay above about 6 a day while the Mac is awake, the remaining cause is network (Wi-Fi) or the MetaQuotes-Demo server; compare on a broker demo.

### 4.2 MT5's own footprint

Settings (MT5 window, then restart MT5):
- Tools › Options › Charts › Max bars in chart: **100000**. The EA exports 400 bars; Unlimited was only for the one-off history export.
- Tools › Options › Server: untick **Enable news**. The engine uses its own calendar export.
- Keep one chart (EURUSD H1 with FX_Execution), no indicators, no extra tick charts. Minimising MT5 is fine once App Nap is disabled.
- Market Watch: right-click › Hide All, then show only the six traded pairs. Fewer symbols means fewer tick updates to translate.
- Leave the MT5 AI Assistant idle and its MCP server disabled during trading; use them only in development mode.

EA work per second (protected path): `GetATR` used to create and release an indicator handle on every timer tick for each managed position. EA build `2026.11.05-1` caches one handle per symbol and releases the handles in `OnDeinit` (staging compile only; deploy after review and owner approval). The market-data export already writes bars only on new bars. Measure MT5 CPU with `host_check` before and after the settings above and after deploying the build.

### 4.3 Memory contention

Kiro and the browser are the swing factor (≈ 7.8 GB together today). In trading mode:
- One Kiro window at most, no running workflows or multi-agent sessions. Better: quit Kiro during trading hours.
- Browser closed or a few tabs.
- Reboot before a trading week (or after a heavy development day). macOS only returns swap gradually; a reboot resets it to near zero.

### 4.4 Replays and research jobs

Already fixed in code (STATUS issues 3 and 4): candidate cache, at most 2 workers, niced, run through `python -m validation.detached_job` with a 3-hour limit and leftover-process cleanup. Operating rule: replays run only in research mode (section 5). `host_check --mode trading` blocks while any replay or research process is alive.

## 5. Operating route: three modes and a schedule

Decisions only happen inside the London/New York sessions (`core/filters/session_filter.py`). In Nairobi time (UTC+3, no daylight saving):

| Period | UTC | Nairobi | Mode |
| --- | --- | --- | --- |
| Trading window, November–March | 07:00–21:00 | **10:00–00:00** | Trading |
| Trading window, late March–October | 06:00–20:00 | **09:00–23:00** | Trading |
| Weekday nights | 21:00–07:00 | 00:00–10:00 (winter) | Research or development |
| Weekend (market closed Fri ~22:00 to Sun ~22:00 UTC) | | Sat 01:00 to Mon 01:00 | Development and research |

The 15 minutes before a window opens are for switching modes and running `host_check`.

### Mode T: trading

- Runs: MT5 + FX_Execution, engine (`main.py --mode demo` via the watchdog agent), keep-awake, calendar refresh.
- Does not run: replays, workflows, extra Kiro windows, heavy browser use.
- Host: AC power, lid open, App Nap off for MT5, swap below 1.5 GB at the start of the day.
- Start: `scripts/macos/trading_mode.sh on`, then fix every BLOCK that `host_check` reports, then `scripts/macos/install_launchd.sh --engine`.
- During the day `python -m validation.host_check` and `python -m validation.burnin_status` can be run at any time; both are read-only.
- While it runs, the engine re-checks the host-guard rules (section 6) and refuses new entries on a BLOCK. `burnin_status` shows the missed M15 cycles per day; `python -m validation.gate_h1` shows the gate H1 conditions.

### Mode D: development

- Kiro, agents, workflows, browser, tests. The engine stays stopped unless a test wave needs it.
- MT5 may stay open with FX_Execution attached so open positions keep their management and feedback, or it can be closed when the strategy uses broker-side brackets only (section 6).

### Mode R: research

- Replays and research tools through `validation.detached_job`, 2 workers (3 if MT5 is closed and nothing else is running), in the night window or at weekends.
- `host_check --mode research` warns but does not block on trading-only rules.
- End: check `done.json` and that no worker processes remain (`detached_job status`).

Switching back: `scripts/macos/trading_mode.sh off` removes the engine, watchdog, calendar and keep-awake agents so the Mac can sleep and nothing trades.

## 6. Make the system tolerant of a laptop host

A laptop will occasionally lose power, network or the terminal. The system is already fail-closed for entries: a stale snapshot or stale bars stop new decisions (`FILE_BRIDGE_*`, `STATE_STALE`). Restarts do not duplicate orders (submission markers) or repeat the current bar (STATUS issue 5). What remains is open positions while the host is down:

| Exit style | Broker holds while the host is down | Exposure if the Mac is down for hours |
| --- | --- | --- |
| Fixed bracket (SL and TP at the broker): H-006, H-007, H-009, `fixed_tp` | Stop and target | None beyond the planned trade |
| `trend_pullback_v1` TRAIL (current default) | Stop only; break-even, partial and trail need the EA | Capped at the original 1R stop, but no target and no trail |
| HYBRID | Stop and target; trail needs the EA | Planned trade, less the trail |

Rule for live use from this Mac: prefer strategies whose exits are fully broker-side (fixed brackets). All three Phase 2b strategies already are. Trailing and break-even remain allowed, but the system must not depend on them to stay within risk.

Host tolerance, built 2026-11-05 on branch `host-tolerance` (ADR 0015; protected path marked):
1. **Engine host guard** (`application/host_tolerance.py`, rules in `core/host_rules.py`, shared with `host_check`). The guard checks the host at engine start, every 5 minutes, every 30 seconds while blocked, and at the first poll inside each day's trading window. It blocks on battery power, sleep not prevented, App Nap not disabled for MT5, a replay or research process running, or an account snapshot older than 30 s. A failed probe also blocks: a `pgrep` that does not run is never read as "no replay running". On a BLOCK the decision cycle refuses new entries (funnel stage `HOST`, reason `HOST_GUARD_BLOCK`) and records a `HOST_GUARD_BLOCK` risk event. It records `HOST_GUARD_RECOVERED` when the host passes again. These two events send no Telegram risk alert; the outage alert (item 3) reports a stale bridge. Open positions, feedback, exits and closure recovery keep running. Each day's first check inside the window also runs the other rules that can BLOCK in `host_check` (memory, disk, MT5 running, connection drops) and is recorded as `HOST_GUARD_DAILY_CHECK` (`scope=host_check`), with swap, for gate H1. Those extra rules are recorded but do not block entries. The guard runs only in demo or live mode on macOS, never in mock mode. It makes about nine short command calls per check, with no MT5 CPU sampling. In demo mode a stale snapshot leaves the demo account unverified: a running engine blocks new entries but keeps processing feedback, exits, closure recovery, the guard and the outage alert (review finding HT-007), and a cold start is refused until a fresh demo snapshot exists. A confirmed live account still stops the demo engine before any processing. Both paths run the guard first, so the BLOCK is recorded. A `pgrep` killed by a signal (negative return code) also counts as a failed probe (HT-008). The blocked state is kept in the database, so the engine that starts once the host passes records the recovery.
2. **Missed-cycle counter** (`validation/cycle_counter.py`, printed by `burnin_status`). Per weekday it reports expected M15 cycles (in-session slots), evaluated cycles, cycles blocked by the host guard, and missed cycles (expected minus evaluated). Only engine rows count, not mock runs. Holidays show as missed cycles.
3. **Outage alert** (`OutageMonitor`). It sends one Telegram message when the account snapshot or the bar exports are more than 2 minutes stale during trading hours, and one when they recover. Bar exports are measured on the six M15 bar files themselves, plus the `status.json` heartbeat, so a failed export is caught while `status.json` keeps updating. With no alert channel configured it does nothing. The engine and the watchdog (every 30 s) share the episode state in `fx_ai_engine/database/outage_alert_state.json`, so a restart during an outage sends nothing new. The watchdog matters because demo mode stops the engine once the snapshot is 30 s old. The engine's own risk alerts for the same stale file (`STATE_RECONCILIATION_FAILED`, `STATE_STALE`, `LIVE_EQUITY_STALE`, `LIVE_EA_STATUS_UNAVAILABLE`) are recorded without a Telegram message while the outage alert covers that file. The restart after an outage sends no "FX AI Engine Started" message. Later starts send it as usual.
4. **EA CPU** (protected): `GetATR` uses a cached `iATR` handle per symbol, released in `OnDeinit`; EA build `2026.11.05-1`. Not deployed.
5. **Reconcile after downtime**: `tests/test_reconcile_after_downtime.py`. A broker-side TP or SL close during an 8-hour engine outage reconciles to closed exactly once on the first loop after restart, before any decision cycle. A second loop changes nothing, and the close is still applied while the host guard blocks entries.

## 7. Live trading on this Mac: what must be true first

Host conditions (gate H1, new):
- 5 consecutive trading days in Mode T where `host_check` has no BLOCK at the start of each window.
- MT5 connection drops of 6 or fewer per day during the window.
- No Sleep or DarkWake events in `pmset -g log` during the window.
- Swap below 1.5 GB at the start of each window, and the missed-cycle count at zero.

Evaluator: `python -m validation.gate_h1 [--since YYYY-MM-DD] [--json]` (read-only; exit 0 only on PASS). For each completed weekday window it applies these conditions to recorded data:
- the engine's `HOST_GUARD_DAILY_CHECK` within 15 minutes of the window opening, with `scope=host_check` and status PASS. That record covers every rule that can BLOCK in `host_check` trading mode, including memory, disk, MT5 running and connection drops. A record without that scope is NO_DATA;
- swap from that record;
- connection drops in the MT5 terminal logs, whose times are local; `--log-utc-offset-hours` overrides the offset;
- Sleep and DarkWake entries in `pmset -g log`, or a saved copy given with `--pmset-log`;
- the missed-cycle count.

A condition without data is NO_DATA and does not pass. H1 needs the 5 most recent completed trading days to pass in a row. On 2026-11-05 it reports NOT_YET: the engine has not yet run with the guard, and 11-04 had 20 Sleep/DarkWake entries in the window.

Physical setup (Nairobi-specific; owner to confirm what applies):
- A small UPS for the router/modem. The laptop battery covers the Mac during a power cut, but not the internet connection.
- A backup connection, such as a phone hotspot, configured as a second Wi-Fi network.
- Charger connected at all times in Mode T, with macOS battery longevity management left on.

Non-host blockers (already in `docs/STATUS.md`; the host route does not remove them):
- A strategy that passes its pre-registered replay test (Phase 2b, issue 29). The current strategy loses about 0.35R per trade.
- Pre-live gate and risk level in SRS v1.1 (ADR 0007 open item), and a live mode (issue 19; `RequireDemoAccount=true`, issue 15).
- Broker checks: hedging vs netting (issue 13), symbol suffixes (issue 14), cent-account units (issue 27).
- Account size versus minimum lot (issue 24): a $10 standard account rejects most signals at 0.01 lot.

## 8. When to move off the Mac

A Windows VPS (about $17–40 a month, see `2026-11-04-funded-account-and-broker-options.md`) runs MT5 natively without Wine or Rosetta and has no sleep, battery or Wi-Fi exposure. For a $10–100 account that cost is larger than any plausible monthly profit, so the Mac route is the economical one at the start. The MQL5 built-in VPS runs only MQL5 programs migrated from the terminal, so it cannot run the Python engine. Move to a VPS when either of these holds:
- the account is large enough that the VPS cost is under about 5% of expected monthly profit; or
- gate H1 cannot be met on this Mac after the fixes above (for example, the drops persist while the Mac is awake).

The code needs no change to move: on Windows the engine can use the same file bridge, or the native `MetaTrader5` package.

## 9. Checklist

Once:
- [ ] `scripts/macos/trading_mode.sh on` (App Nap off for MT5, keep-awake agent installed).
- [ ] MT5: Max bars 100000, news off, one chart, Market Watch reduced to 6 pairs; restart MT5; re-attach FX_Execution (build `2026.11.05-1` once the owner approves it; until then the deployed build).
- [ ] Optional: `sudo pmset -c sleep 0 powernap 0 standby 0 disksleep 0`.
- [ ] Reboot, then run `python -m validation.host_check` and confirm no BLOCK.

Each trading day:
- [ ] Charger in, lid open, Kiro and browser closed or minimal, no replays.
- [ ] `python -m validation.host_check` before the window opens.
- [ ] Engine running via the watchdog; check `validation.burnin_status` once or twice (missed cycles should stay at 0).
- [ ] After a week in Mode T: `python -m validation.gate_h1`.

After trading / weekends:
- [ ] `scripts/macos/trading_mode.sh off` if switching to heavy development; run replays through `validation.detached_job`.
