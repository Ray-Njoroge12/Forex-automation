# Funded accounts and brokers: compatibility with this system (2026-11-04)

Purpose: decide which kinds of funded account can run this system on the Mac in use, and what has to change first. Research date 2026-11-04. Broker and prop-firm terms change often; confirm each item on the provider's own site before depositing. Content from web sources is summarised, with links inline.

**Assumption to confirm:** the machine's timezone is East Africa Time and the GitHub handle suggests Kenya, so I looked at brokers available to Kenyan residents. If you are elsewhere, the broker list changes; the compatibility checks in sections 2 and 3 do not.

---

## 1. Recommendation

1. **Do not fund an account yet.** In replay the current strategy loses about 0.35R per trade after costs, with a Monte Carlo median of -8% after 30 trades and -19% after 100 at the locked 3.2% risk (`2026-11-04-system-capability-and-readiness.md` section 4). A funded account today would be a paid test of execution, and the expected result of the strategy itself is a loss.
2. **Open a demo account at the broker you would fund, now.** It costs nothing and uses real prices. It answers the broker-specific questions in section 3 (symbol names, hedging, commission, filling mode) before money is involved.
3. **Choose a raw-spread MT5 account at a CMA-regulated broker over a prop firm.** The system's locked risk limits exceed prop-firm loss limits (section 4), and the EA is built to refuse non-demo accounts until you change that deliberately.
4. **Fund only after a release passes the replay gate** (R1 to R5, positive expectancy after 1.5x costs on holdout), the same release has run on the broker's demo, and SRS v1.1 defines the pre-live gate. Start at a balance of at least $250 and at the lower risk the D3 default points to (1.0% to 1.5%).

---

## 2. What this system needs from an account

Verified in the code on 2026-11-04.

| Requirement | Why | Source in the code |
| --- | --- | --- |
| MetaTrader 5, hedging account | The EA tracks each position by ticket in per-position management slots (up to 10). It never reads `ACCOUNT_MARGIN_MODE`, so on a netting account it would not refuse to run; how it would misbehave is untested | `mt5_ea/FX_Execution.mq5` (no margin-mode check) |
| Expert Advisors allowed, no dealing-desk delay on orders | The EA is the only order placer | AGENTS.md invariant 1 |
| Plain symbol names: `EURUSD GBPUSD USDJPY AUDUSD USDCAD USDCHF` | The EA's symbol list is hard-coded without any suffix | `FX_Execution.mq5` lines 32 and 832 |
| USD account currency | The bridge defaults currency to USD and the sizing and pip values assume it | `core/file_bridge_connection.py` |
| Raw-spread pricing with commission, near $3.50 per lot per side | The replay cost model is 0.2 pip slippage plus $7.00 per lot round trip. A commission-free Standard account has wider spreads (Pepperstone's published EURUSD average is about 1.1 pips against 0.1 on Razor) and would cost far more than the model assumes | `backtesting/unified_replay.py` cost scenarios |
| Market execution with filling mode FOK, IOC or return | `ResolveFilling` picks the symbol's supported mode | `FX_Execution.mq5` line 951 |
| Stop level and freeze level small enough for 8 to 20 pip stops | The EA checks both before sending | `FX_Execution.mq5` lines 986 to 987 |
| Demo trade mode today | `RequireDemoAccount = true` makes the EA drop every signal on a real account | `FX_Execution.mq5` line 1551 |

Minimum balance. The engine sizes 3.2% risk per trade. Lots round down to 0.01, so small accounts drift from the intended risk (my calculation, EURUSD-like pip value of $10 per lot):

| Balance | Stop 8.2 pips | Stop 20 pips |
| --- | --- | --- |
| $100 | 0.03 lots, 2.46% risk | 0.01 lots, 2.00% risk |
| $250 | 0.09 lots, 2.95% | 0.04 lots, 3.20% |
| $500 | 0.19 lots, 3.12% | 0.08 lots, 3.20% |
| $1,000 | 0.39 lots, 3.20% | 0.16 lots, 3.20% |

From $250 up, rounding changes the risk by less than half a percentage point.

---

## 3. Device: macOS on Apple silicon

MetaQuotes provides an official macOS installer that downloads Wine and installs MetaTrader inside it; it supports Apple processors from M1 up and macOS Catalina or later ([MetaTrader 5 help](https://www.metatrader5.com/en/terminal/help/start_advanced/install_mac)). That is the setup running here. There is no native macOS build ([signalforge](https://www.signalforge-ai.com/es/mt5-en-mac/)).

What this means:

- The file bridge is independent of the broker. The EA writes into `MQL5/Files/bridge` and the Python engine reads it, so any broker whose MT5 server the Mac terminal can log into works. The Python `MetaTrader5` package, which is Windows-only, is not needed.
- You switch brokers by logging the same terminal into the new server (File, Login to Trading Account) and re-attaching the EA. The EA's `ExpectedPolicyHash` pin and magic number stay as they are.
- Wine on a laptop is acceptable for demo development. For real money the plan (Phase 7) calls for a Windows VPS, because the Wine route has no stability guarantee, the Mac sleeps and loses network, and a stopped engine leaves open trades unmanaged except by the EA. The engine and the terminal must sit on the same machine because they share a folder, so the Python engine moves to the VPS too.
- VPS prices in the sources I found: about $17 to $40 per month for a dedicated Windows plan ([forexvps.net](https://www.forexvps.net/resources/best-mt5-vps-providers/), [vpsforextrader](https://www.vpsforextrader.com/blog/best-vps-for-metatrader-5/)). Cheap $3 to $7 plans often cannot run MT5 reliably. Some brokers waive the fee above an activity threshold ([forexvps.net](https://www.forexvps.net/resources/free-metatrader-vps-setup/)).
- Windows runtime for the Python side is not verified in this session. `watchdog.py` is written for Windows Task Scheduler, and an earlier `.mcp.json` path points to a Windows layout, which suggests it ran there before. Test it on the VPS before relying on it.

---

## 4. Option A: prop-firm funded accounts

Not compatible with the locked SRS limits.

| | This system (SRS v1, locked) | FTMO 2-Step | The5ers (2-step) |
| --- | --- | --- | --- |
| Risk per trade | 3.2% | set by the trader | set by the trader |
| Daily loss | halts at 8% | **5% maximum** | **5% maximum** |
| Total drawdown | halts at 20% | **10% maximum** (static) | **10% maximum** |
| Profit target | none | 10% then 5% | 8% on 2-step |

Sources: [FTMO rules, October 2026](https://tigerfunded.com/blog/ftmo-prop-firm), [FTMO review](https://tradingfunder.com/firms/ftmo/), [The5ers rules](https://tradingfinder.com/props/the-5ers/rules). FundedNext and E8 allow EAs with fewer strategy limits ([FundedNext](https://help.fundednext.com/en/articles/8020763-is-ea-allowed-in-fundednext), [E8 and FTMO](https://fundedtrading.com/prop-firms-for-expert-advisors-ea-trading/)). FTMO restricts high-frequency trading above about 2,000 server requests per day and applies closer scrutiny to martingale and grid strategies ([xaubot](https://xaubot.com/prop-firm-ea-rules/)); this system, with a handful of trades per day, would not be expected to trigger either.

Why it fails here:

- Two consecutive losses at 3.2% are 6.4%, above a 5% daily limit. The system's own daily halt at 8% would never fire first.
- The system's drawdown halt at 20% is double the 10% a firm allows. In replay with account halts on, the strategy reaches 20% drawdown within two weeks.
- A challenge needs a 10% profit target. The strategy has negative expectancy.
- Vendors advise cutting a 2% to 3% default risk to 0.5% to 0.75% inside these limits ([fortraders](https://fortraders.com/blog/best-mt5-expert-advisors-eas-for-funded-accounts)). That needs an SRS revision, which the owner has not requested.
- Not checked: whether a prop firm's MT5 account reports demo or real trade mode. That decides whether `RequireDemoAccount` blocks it. Ask the firm.

Revisit this option only after SRS v1.1 sets risk per trade near 0.5% to 1.0% and a release shows positive expectancy.

---

## 5. Option B: own-capital account at a regulated raw-spread MT5 broker

Compatible, after the fixes in `2026-11-04-system-capability-and-readiness.md` P1 items 7 to 12.

Kenya's Capital Markets Authority licenses retail forex brokers. The sources I read list Pepperstone, FP Markets, IC Markets, HFM, AvaTrade, FXPesa and Scope Markets as licensed ([forexbrokers.com](https://forexbrokers.com/guides/kenya), [fxleaders](https://www.fxleaders.com/forex-brokers/forex-brokers-by-country/forex-brokers-kenya/)). Check the exact legal entity on the CMA register before depositing; one source says IC Markets' sign-up can default Kenyan residents to its Seychelles entity.

| Broker (raw account) | Cost | EA friendliness | Regulation | Notes |
| --- | --- | --- | --- | --- |
| **Pepperstone, Razor MT5** | Spreads from 0.0 pips, commission from $3.50 per lot per side ([costs](https://pepperstone.com/en/trading/costs-and-fees)). A third-party test lists EURUSD at 0.1 pips and $7 round trip ([compareforexbrokers](https://www.compareforexbrokers.com/reviews/pepperstone-review/fees/)) | States no dealing-desk intervention on algo orders and 99.87% of orders executed ([Pepperstone](https://pepperstone.com/en/insights/guides/automated-trading/why-use-your-expert-advisor-with-pepperstone)) | Pepperstone Markets Kenya on the CMA register per fxleaders; also FCA and others | Closest match to the replay cost model |
| **IC Markets, Raw Spread MT5** | EURUSD average 0.1 pips, $3.50 per lot per side ([IC Markets Kenya](https://www.icmarkets.com/ke/en/trading-accounts/overview)). Tested at 0.19 pips plus $7.00 ([compareforexbrokers](https://www.compareforexbrokers.com/reviews/ic-markets-review/)) | States no minimum order distance and a freeze level of 0 on MT4 and MT5 ([IC Markets](https://www.icmarkets.com/ke/en/trading-accounts/standard-account)) | IC Markets (KE) holds CMA licence 199 per its own filing; the global company is under Seychelles FSA | IC Markets' help centre states no minimum deposit |
| **FP Markets** | Not checked | A 2026 award for MetaTrader and algo trading is claimed by one Kenyan review site ([kenyaforexfirm](https://kenyaforexfirm.com/best-brokers/fp-markets-review/)); I did not confirm it at the source | CMA-licensed per forexbrokers.com | Needs the same checks |

I could not verify, for any of the three, the exact MT5 symbol names, whether the account type is hedging, or whether Kenyan residents get the CMA entity's terms. Those three facts decide compatibility and are free to check on a demo account.

Do not use a commission-free Standard account: its spreads would add most of a pip to every trade, and the strategy already loses to costs. Avoid cent accounts: the engine's lot and risk logic assume standard lots.

I did not evaluate Exness, XM or other brokers popular in Kenya. Several brokers add symbol suffixes by account type; that would break the EA until item 8 is done.

---

## 6. Steps to a funded account, in order

| Step | Gate | Owner action |
| --- | --- | --- |
| 1. Open a demo at the chosen broker: raw account, MT5, USD, hedging | Market Watch shows plain `EURUSD`-style names; account type reads hedging; commission matches the table | Open the account; log the Mac terminal into it |
| 2. Repeat the EA acceptance matrix and run R7 on that demo | All cases match; R7 spread and slippage within the 1.5x cost model | Attach EA build `2026.11.04-1` |
| 3. Add EA guards for netting accounts and symbol names | Second-model review and your approval (protected path) | Approve the change |
| 4. Strategy redesign in replay | Positive expectancy after 1.5x costs on holdout; R2 gate PASS; R3 not failing | Decide the redesign scope |
| 5. SRS v1.1 | Pre-live gate defined (ADR 0007); D2, D3, O1 to O5 decided | Approve SRS v1.1 |
| 6. Windows VPS with engine and terminal; test restart and outage | Engine survives a reboot and a network drop with open trades | Choose and pay for the VPS |
| 7. Fund a small balance (at least $250) and set `RequireDemoAccount=false` | Written approval; first trades at minimum risk | Deposit, approve the EA change |
| 8. Compare live fills to the demo with R7 and R8 before any increase | Live cost within the replay's 1.5x assumption | Review the reports |

Each step uses tools that already exist.

---

## 7. Prop-firm considerations (second pass, 2026-11-04)

Recorded for later decisions. Nothing here is a recommendation to buy. Sources are linked; affiliate-funded comparison sites are marked.

### 7.1 What "funded" means

- A funded prop account is usually **simulated**. The firm pays rewards from fee revenue, and fills are the firm's own, not a real broker's ([propfirmhero](https://propfirmhero.com/prop-trading-course/simulated-vs-live-accounts), [myfxbook](https://www.myfxbook.com/articles/what-is-a-funded-account-in-prop-trading/16)). A prop account therefore does not replace a broker demo as the last test before real money.
- There is no capital to put up, but there is a **challenge fee**. FTMO 2-Step costs EUR 89 to EUR 1,080 by account size ([proptradingvibes](https://proptradingvibes.com/blog/ftmo-account-sizes-pricing)); the fee is refunded with the first reward on the 2-Step only ([FTMO](https://ftmo.com/en/faq/what-fees-do-you-charge-and-are-they-recurrent/)).
- Industry data covering 100,000+ traders at 10 firms puts passing at about 14% and ever being paid at about 7% ([FPFX Tech via fortunly](https://fortunly.com/statistics/prop-firm-challenge-pass-rate-statistics/)).

### 7.2 Zero-cost option

FTMO Free Trial: 14 days, simulated, on MT4, MT5 or cTrader, up to a $200,000 account, as many trials as you like (one at a time). Profit target 5% (2-Step phase 1 is 10%) and 2 minimum trading days. A rule breach ends it at no cost ([FTMO](https://ftmo.com/en/ftmo-free-trial/)). It pays nothing. Its value to this project is integration testing: whether the MT5 account reports demo mode, symbol names, hedging, commissions.

### 7.3 MT5 availability and Kenya

- MetaQuotes cut off most grey-label MT5 setups for prop firms in 2024; MT5 survives at firms with their own licence or a broker deal ([fundedtrading.com](https://fundedtrading.com/best-mt5-prop-firms/), affiliate site). Firms listed there with MT5: FXIFY, FundedNext, Alpha Capital Group, E8 Markets, Blueberry Funded, Maven, FunderPro, Funded Trading Plus, The5ers, Instant Funding. Availability changes; check each firm's own platform page.
- PropFirmMatch lists 33 firms open to Kenyan traders, including FTMO, FundedNext, The5ers, E8 Markets, Alpha Capital, FundingPips, Maven, Instant Funding and Funded Trading Plus ([propfirmmatch](https://propfirmmatch.com/prop-firm-lists/countries/kenya)).
- Kenya is not on FTMO's CFD restriction list in a third-party capture dated 2026-09-07; it is excluded from FTMO Futures ([fundingtraders](https://fundingtraders.com/blog/ftmo-banned-countries-list/), a competitor's page). FTMO's own page timed out when fetched; **confirm on ftmo.com**.

### 7.4 Rules that matter for this system

| Topic | Finding | Source |
| --- | --- | --- |
| Loss limits | FTMO 2-Step: 5% max daily loss, 10% static max loss; daily limit counts equity including open positions and resets at midnight CET | [FTMO academy](https://academy.ftmo.com/lesson/maximum-daily-loss/), [stonkjournal](https://stonkjournal.com/articles/ftmo-review) |
| EAs | FTMO allows algorithmic trading and EAs; a third-party EA shared with other traders can lead to denial of the funded account; hyperactive EAs and third-party account access are forbidden | [FTMO](https://ftmo.com/en/faq/which-instruments-can-i-trade-and-what-strategies-am-i-allowed-to-use/) |
| Consistency | FTMO added a best-day consistency rule in October 2026 (50% on 1-Step, 30% on Instant) | [alphaexcapital](https://www.alphaexcapital.com/prop-trading/ftmo-faqs), third-party |
| Recommended risk | FTMO suggests 1% to 1.5% per trade (a best practice, not a limit) | [FTMO](https://ftmo.com/en/blog/why-ftmo-monitors-certain-patterns-in-trading-behaviour/) |
| Other firms | FundedNext: EAs allowed on accounts under $50,000, no strategy limits stated. The5ers High Stakes: 6% trailing drawdown and a 2-minute news window. E8: 5-minute news ban on funded accounts, best-day rule 35 to 40%. Alpha Capital: 2-minute average trade duration rule. FunderPro: static 10% drawdown but a 2026 Trustpilot warning | [FundedNext](https://help.fundednext.com/en/articles/8020763-is-ea-allowed-in-fundednext), [fundedtrading.com](https://fundedtrading.com/best-mt5-prop-firms/) |

### 7.5 Would this strategy pass?

Simulation of 6,406 replay trades resampled by day against FTMO-style rules (+10% target, 5% daily loss from start-of-day equity, 10% static max loss); 6,000 attempts per row. Script and output: `fx_ai_engine/artifacts/research/small-account-sim/prop_challenge_sim.py`, `output_prop_challenge.txt`.

| Risk per trade | P(pass) in 30 days | Main failure |
| --- | --- | --- |
| 0.5% | 0% | max-loss breach 84% |
| 1.0% | 0.5% | daily-loss breach 71% |
| 1.5% | 1.3% | daily-loss breach 88% |
| 3.2% (locked SRS risk) | 4.7% | daily-loss breach 88% |

Both phases at 1.0% risk: about 0.01%. Control: the same trades shifted to zero expectancy would pass 32% to 44% of attempts at 0.5% to 1.0% risk. The shortfall is the -0.35R edge, not the rule set. The simulation ignores the engine's own halts and uses the replay's 7 trades a day.

### 7.6 Integration considerations (open)

1. **Demo flag.** `RequireDemoAccount` and the demo-start check decide on `ACCOUNT_TRADE_MODE`. A firm's simulated MT5 account may report demo or real. Not verified for any firm; the free trial shows it in `status.json`.
2. **Limits.** The locked SRS limits (3.2% risk, 8% daily, 20% drawdown) exceed firm limits. A prop policy mode would need about 0.5% to 1% risk, a 3% daily stop and a 6% to 7% drawdown halt. Protected change; owner approval and an SRS addendum.
3. **Reset clock.** FTMO's daily loss resets at midnight CET; the engine resets on UTC.
4. **Equity vs closed P&L.** FTMO's daily loss includes floating P&L. Whether the Hard Risk Engine's daily stop does has not been checked.
5. **Account hygiene.** Single device and connection without VPNs is advised at some firms, and third-party access is forbidden. Run the terminal on your own machine or VPS and read each firm's terms.
6. **Payouts to Kenya.** Method, fees and timing not verified.

### 7.7 Sequence if this route is pursued

Fix the strategy in replay first. Then use the free trial to test integration. Only after a release passes the replay gate, buy the cheapest challenge at the firm tested, with the prop policy mode in place.
