# Running the system on this Mac: limits, causes and operating route

Date: 2026-11-04. Host: MacBook Pro M2 (Mac14,7), 8 GB RAM, 8 cores, macOS 27.2, MT5 build 6296 under MetaQuotes' Wine (x86-64 translated by Rosetta). Every number below was measured on this machine today unless marked otherwise. Tool: `python -m validation.host_check` (read-only).

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

EA work per second (development backlog, protected path): `GetATR` creates and releases an indicator handle on every timer tick for each managed position. Caching one handle per symbol removes that cost. The market-data export already writes bars only on new bars. Measure MT5 CPU with `host_check` before and after the settings above, then decide whether the EA change is worth it.

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

Development backlog for host tolerance (not built yet; protected paths marked):
1. **Engine host guard.** Run the `host_check` trading rules at engine start and every few minutes. Refuse to route new entries and raise a risk event on BLOCK (on battery, sleep not prevented, App Nap on, replay running). Existing positions are untouched.
2. **Missed-cycle counter.** Count expected vs evaluated M15 decisions per session in `burnin_status`, so host problems show up as a number.
3. **Outage alert.** One message (Telegram is already wired in `core/alerts.py`) when snapshots go stale for more than 2 minutes in trading hours, and when they recover.
4. **EA CPU** (protected): cache the ATR handle per symbol in `ManageOpenPositions`.
5. **Reconcile after downtime.** Already covered by the deal-history export and closure recovery; add a test for "position closed by broker-side TP while the engine was down for 8 hours".

## 7. Live trading on this Mac: what must be true first

Host conditions (gate H1, new):
- 5 consecutive trading days in Mode T where `host_check` has no BLOCK at the start of each window.
- MT5 connection drops of 6 or fewer per day during the window.
- No Sleep or DarkWake events in `pmset -g log` during the window.
- Swap below 1.5 GB at the start of each window, and the missed-cycle count at zero.

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
- [ ] MT5: Max bars 100000, news off, one chart, Market Watch reduced to 6 pairs; restart MT5; re-attach FX_Execution build `2026.11.04-1`.
- [ ] Optional: `sudo pmset -c sleep 0 powernap 0 standby 0 disksleep 0`.
- [ ] Reboot, then run `python -m validation.host_check` and confirm no BLOCK.

Each trading day:
- [ ] Charger in, lid open, Kiro and browser closed or minimal, no replays.
- [ ] `python -m validation.host_check` before the window opens.
- [ ] Engine running via the watchdog; check `validation.burnin_status` once or twice.

After trading / weekends:
- [ ] `scripts/macos/trading_mode.sh off` if switching to heavy development; run replays through `validation.detached_job`.
