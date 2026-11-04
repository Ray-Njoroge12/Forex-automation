# MT5 and demo account check, and live-account feasibility (2026-11-04)

Checked at 10:35 to 10:50 UTC (13:35 to 13:50 EAT) on 2026-11-04, read-only. No credential file was opened and nothing was started, stopped or traded. Evidence labels: **Verified** (read on this machine today), **Source** (web page, linked), **Not verified**.

---

## 1. Verdict

| Question | Answer |
| --- | --- |
| Is MT5 working? | Yes, with three problems: a flaky server connection, a heavily loaded host, and an intermittent calendar feed (section 2) |
| Is the MetaQuotes-Demo account enough to test the system? | Enough to finish **software** testing. Not enough as the last test before real money (section 3) |
| Can a live account be set up from this Mac? | The terminal can log into a real broker. The system **cannot trade it today**: the engine has no live mode, and the EA refuses real accounts by design (section 4) |
| Would it earn money? | Not on current evidence. Replay expectancy is -0.35R per trade after costs (section 5) |

---

## 2. MT5 and host: current state

| Item | Finding | Label |
| --- | --- | --- |
| Terminal | MetaTrader 5 build 6275, "Windows 11 on Wine 11.7", 8 virtual x64 cores (Rosetta), running 1 day 1.5 hours | Verified |
| Mac | MacBook Pro M2 (Mac14,7), **8 GB RAM**, macOS 27.2, on AC power at 100% | Verified |
| Account | MetaQuotes-Demo, login ends 4077, USD, balance and equity 101,609.81, 0 open positions, trading allowed for terminal, EA and account | Verified |
| EA | Attached and writing: account snapshot refreshed every few seconds, bridge status 4 seconds old. Version `2026.11.03-2` (build `2026.11.04-1` not deployed) | Verified |
| Servers ever used | Only MetaQuotes-Demo. No broker server has ever been logged into on this terminal | Verified |
| MT5 MCP server | Reachable at `127.0.0.1:22346` (HTTP 401, needs the API key). Not used | Verified |
| CPU | `terminal64.exe` at 142% CPU and `wineserver` at 41%; load average about 6 on 8 cores. This is the heat you noticed | Verified |
| Memory | 4.9 GB of 6 GB swap in use, 1.6 million swap-outs; 42% free | Verified |
| Sleep | The Mac stays awake only because the display is on ("Powerd - Prevent sleep while display is on"). System sleep is set to 1 minute, display sleep to 10. When the display sleeps or the lid closes, the Mac sleeps and both the EA and the engine stop. The keep-awake agent was uninstalled on 2026-11-03 | Verified |
| Connection | "connection to MetaQuotes-Demo lost" **21 times on 2026-11-03 and 76 times on 2026-11-04 up to 08:43 local**, about 9 per hour overnight. Each reconnects within 1 to 2 seconds through Access Point EU 1. Cause not determined: home network, Wine networking or the demo server | Verified |
| Economic calendar | The EA's export at 10:03 UTC held **0 events** (`calendar.json` is `[]`), where the 2026-11-03 export held 31. `install_calendar` correctly rejects an empty list (`CALENDAR_UNAVAILABLE`), so the engine keeps its installed copy (31 events, valid to 2026-11-17). The feed itself is intermittent | Verified |

Reading: MT5 is healthy enough for development. For anything unattended it is not: the Mac sleeps with the display, memory is exhausted, and the link drops several times an hour.

---

## 3. Is MetaQuotes-Demo enough?

### What the demo account is good for

| Use | Why it works here |
| --- | --- |
| Plumbing: bridge, EA protocol, policy-hash pin, expiry, duplicate and malformed handling | Behaviour does not depend on the broker. 8 acceptance cases already pass |
| Decision pipeline and risk engine on live bars | Same code as replay; parity test passes |
| Reconciliation, restart, database audit | Needs only an account that trades |
| Large balance ($101,609) | Lets you see the engine's full sizing logic without lot-rounding noise |

### What it cannot show

| Gap | Evidence from this terminal | Why it matters for a live account |
| --- | --- | --- |
| **Costs** | Commission is 0.0. Spreads right now: EURUSD 0.1, **GBPUSD 0.0**, USDJPY 0.2, AUDUSD 0.1, **USDCAD 0.1**, USDCHF 0.2 pips. A zero spread is not a market price | Replay costs about 0.3R per trade on a median 8.2-pip stop; demo P&L ignores it. A raw-spread broker charges $7 per lot round trip, about $165 on the 23.53-lot GBPUSD trade |
| **Slippage and rejections** | Instant fills at the quoted price; no requote or rejection recorded | Live brokers fill at a worse price when volatile and sometimes reject. A demo account almost never does ([TradeLocker](https://tradelocker.com/how-to/demo-account-vs-live-account/), [MQL5 article](https://www.mql5.com/en/articles/22998)) |
| **Broker rulebook** | Stops level 0, freeze level 0, filling mode FOK only, lot 0.01 step 0.01 | Brokers differ on filling policy, minimum stop distance and lot step. Mismatches return errors such as invalid filling type or invalid stops, and the Strategy Tester never shows them ([MQL5 article](https://www.mql5.com/en/articles/23327)) |
| **Account type** | Hedging per the 2026-11-03 assessment. The EA never checks | A netting account would break per-ticket management |
| **Symbol names** | Plain names | The EA hard-codes plain names; a suffix breaks it |
| **Balance size** | $101,609 | At $250 lots round to 0.01 and risk drifts from 3.2% |
| **Sample** | 1 trading day, 23 proposals, 1 routed trade (closed by hand, probably) | G1 burn-in needs 5 days; a strategy verdict needs on the order of 118 trades (draft SRS v1.1 proposal, section 2.3) |
| **Feed quality** | Calendar export returned 0 events today; link dropped 97 times in two days | A broker demo shows whether this is the MetaQuotes server or this Mac |

### Conclusion

Keep MetaQuotes-Demo for software testing and burn-in. Before real money, repeat the EA matrix, the fill check (R7) and the burn-in on a **demo account at the broker you would fund**, using its Raw or Razor account. Broker demos expire when idle: 20 days for IC Markets MT5 ([bestbrokers](https://www.bestbrokers.com/reviews/ic-markets/account-opening/)), 60 for Pepperstone ([Pepperstone](https://pepperstone.com/en-af/help-and-support/managing-my-account/does-a-trading-account-expire-if-it-is-not-used-for-some-time/)). Keep the terminal logged in.

A demo cannot validate the strategy's profitability. Test that in replay; the demo's job is to measure execution.

---

## 4. Setting up a live account from this Mac

### What works

- The terminal can log into any broker's MT5 server: File, Login to Trading Account, then type the exact server name the broker gives you ([example guide](https://www.fxgoplus.com/en/how-to-install-mt4-and-mt5-on-mac-os/)). The file bridge does not depend on the broker. Whether a given broker's server appears in this terminal's list is **not verified**; typing the name is the fallback.
- Opening an account is personal and involves your money and identity, so I cannot do it for you. Pepperstone's Kenya page asks for photo ID and proof of address (bank statement or utility bill under three months old, no PO boxes) ([Pepperstone](https://pepperstone.com/en-af/help-and-support/getting-started/)). Pepperstone and IC Markets (KE) both quote a $200 minimum ([brokerchooser](https://brokerchooser.com/broker-reviews/pepperstone-review/pepperstone-minimum-deposit/kenya), [IC Markets KE](https://www.icmarkets.com/ke/en/help-resources/help-centre)).
- Kenya's CMA regulates online forex brokers under the 2017 regulations, requires client-fund segregation and caps leverage at 1:400 on majors ([forexbrokers.com](https://forexbrokers.com/guides/kenya), [regulations](https://cma.or.ke/wp-content/uploads/2023/03/Capital-Markets-Online-Foreign-Exchange-Trading-Regulations-2017.pdf)). Check the exact legal entity on the CMA register before depositing.
- Funding route (M-Pesa, bank, card), withdrawal fees and shilling conversion costs vary by broker and are **not verified** here.

### What stops the system from trading it

| Blocker | Where | Effect |
| --- | --- | --- |
| No live mode in the engine | `main.py` line 970: `--mode` accepts only `smoke` and `demo` | There is nothing to run against a real account |
| Demo-only start | `main.py` lines 1028 to 1031 | `--mode demo` exits with "requires a verified MT5 demo account" on a real one |
| Demo-only EA | `FX_Execution.mq5` line 1551: `RequireDemoAccount = true` | The EA drops every signal on a real account |
| Experiments disabled off demo | `main.py` `_enforce_demo_only_experiment_account_gate` | Only the core SRS policy would run (the intended behaviour) |
| SRS v1 §12 | "No live deployment before successful 30-day demo" | ADR 0007 dropped the 30-day demo for development and left the replacement gate open |
| No netting or symbol guard | EA | Silent failure on some brokers |
| No unattended host | Section 2 | The EA manages open trades only while the Mac and terminal run |

Removing the first three blockers means changing `main.py` and the EA (protected path) and defining the pre-live gate in SRS v1.1. That is a deliberate build, with your written approval, and it should add its own safeguards: a separate `live` mode that needs an explicit confirmation, a cap on lot size and number of trades, and a loss limit that stops the engine.

### Host for real money

Do not run live capital on this laptop. It sleeps with the display, is out of memory and drops its connection. The plan already calls for a Windows VPS (about $17 to $40 a month, see the broker doc), with the engine and terminal on the same machine. Running MT5 on a Mac is possible, but only a VPS or hosted setup keeps running when the lid is closed ([PineConnector comparison](https://www.pineconnector.com/blogs/pico-blog/mt5-on-mac-2026-compared)).

---

## 5. Would it earn money?

On current evidence, no.

- Replay: -0.35R per trade after costs over 5,622 development trades, -0.36R on the last six months. Every one of 18 variants is negative. Before costs the entry rule is about -0.05R, which is no better than random direction.
- At the locked 3.2% risk per trade, the replay's Monte Carlo gives a median ending balance of about -8% after 30 trades and -19% after 100, with the 20% drawdown halt hit in roughly two of three 100-trade runs.
- For context, Pepperstone Kenya's own risk warning says 74% to 89% of retail margin accounts lose money ([Pepperstone Kenya T&Cs](https://eu-assets.contentstack.com/v3/assets/bltaec35894448c7261/blt93b51003a35b4a3a/PEP_KENYA_Ts&Cs.pdf)).

The one winning engine trade (+$1,576.51) was closed outside the EA, so it is not evidence about the strategy.

If you still want to see live execution before the strategy is fixed, the only defensible version is a **cost-measurement pilot**: about $200 to $250, risk at or below 1% per trade (about $2 to $2.50), a fixed small number of trades and a total loss limit agreed in advance. Its purpose is to measure real spread, commission and slippage with the R7 tool, and its expected result is a small loss, not profit. It still needs the live-mode build above.

---

## 6. Recommended order

1. Stop relying on this Mac for long runs. Re-enable sleep prevention only for owner-started test waves.
2. Open a **demo** account at the broker you would use (Raw or Razor, MT5, USD, hedging). Log the terminal into it. Check symbol names, hedging, commission and filling mode.
3. Attach EA build `2026.11.04-1`. Repeat the EA matrix and a 5-day burn-in there. Run R7 for costs.
4. Investigate the connection drops and the empty calendar on that demo, to see whether they follow the machine or the MetaQuotes server.
5. Redesign the strategy in replay (Phase 2b) until a release passes the R1 to R5 gate.
6. Define the pre-live gate in SRS v1.1 and build the guarded live mode.
7. Then, and only then, fund the pilot on a VPS.

Items 2 and 3 cost nothing and can start now.
