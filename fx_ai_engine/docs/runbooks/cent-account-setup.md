# Runbook: set up a cent account in MT5 and connect it to the system (demo first, then live)

Written 2026-11-04. Target: a $10 cent account on the MT5 terminal on this Mac, feeding the system through the file bridge. Commands run from `fx_ai_engine/` with `PY=../.venv/bin/python`. Steps marked **[you]** need a browser, the MT5 window or a personal decision. Content from web sources is summarised; broker terms change, so confirm each item on the broker's own site.

## 0. Read this first: what works today and what does not

I tested the Python side against a temporary copy of the bridge converted to a $10 cent account (1,000 USC). Nothing in the live bridge folder was changed.

| # | Finding | Evidence | Effect |
| --- | --- | --- | --- |
| 1 | **Broker symbol suffixes break the system.** Exness Standard Cent symbols carry a `c` suffix, for example `EURUSDc` ([Exness help](https://get.exness.help/hc/fr/articles/17537782786588)). The EA and the engine hard-code `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`, `USDCHF` (`FX_Execution.mq5` lines 32 and 832; `application/market_data_service.py` line 26). There is no mapping setting | Code search: no suffix or symbol-map support anywhere | On a suffixed account the EA cannot select or export any symbol, so no market data reaches the engine and nothing trades |
| 2 | **Cent unit handling is inconsistent.** The file bridge passes the account snapshot through in cents (`balance 1000.0`, `currency USC`). The fixed-dollar-risk checks convert the balance to dollars (10.00) but read the symbol's tick value in cents | Test on the temp cent bridge: `evaluate_trade_feasibility` for EURUSD, 25-pip stop, 1.5% risk returned `REJECTED_LOT_PREROUTE` with `balance=10.00`, `risk_amount=0.15`. The correct lot there is 15 USC / (25 pips x 10 USC per pip per lot) = 0.06, which is feasible; the check computes 0.0006 | Any fixed-dollar-risk mode (`preserve_10`) would reject every trade on a real cent account. Percent-risk modes skip that check, so they are not affected |
| 3 | **`preserve_10` cannot start on the file bridge.** `get_preserve_10_approval_facts` returns `FILE_BRIDGE_PRESERVE_10_UNSUPPORTED` | `core/file_bridge_connection.py` | The repo's own $10 mode is unusable on this Mac today |
| 4 | **The EA refuses real accounts and the engine has no live mode** | `RequireDemoAccount = true`; `main.py --mode` is `smoke` or `demo`, and demo exits on a non-demo account | Every live step below is blocked until a guarded live mode is built and approved |
| 5 | **Spread cap.** The EA rejects a signal when spread exceeds `min(MaxSpreadPips = 2.0, signal cap)` | `FX_Execution.mq5` | A commission-free cent account with spreads near 1 to 2 pips will reject some signals. Measure it on the demo |
| 6 | **Hedging is never checked** | The EA does not read the account margin mode | Confirm hedging on the demo before trusting position handling |

Consequence: connecting a cent **demo** account and reading everything the system reports about it works today and is worth doing. Letting the system **trade** a suffixed cent account needs the code changes in section 5 first.

---

## 1. Choose the broker and confirm four facts

Candidates found in the research (not recommendations):

| Broker | Cent account facts | Source | Not verified |
| --- | --- | --- | --- |
| **Exness** | Standard Cent on MT4 and MT5; balance in USC ($10 shows as 1,000 USC); 1 cent lot = 100,000 cents = $1,000 notional; market execution without requotes; opens from $10; Kenyan entity licensed by the CMA | [Exness help](https://get.exness.help/hc/fr/articles/17537782786588), [exness.ke](https://www.exness.ke/standard-accounts/), [tradingbeasts](https://tradingbeasts.com/exness-cent-account-review/) | Whether the Kenyan entity offers the Cent account; M-Pesa; spreads; commission |
| **HFM** | Cent account, cent lot = 0.01 standard lot = 1,000 units, swap free, margin from 10 cents; Cent, Zero and Premium accounts open from $0 | [HFM](https://www.hfm.com/int/en/trading-accounts/cent-account), [compareforexbrokers](https://www.compareforexbrokers.com/reviews/hfm/) | Symbol suffix; Kenyan entity terms |
| **TMGM** | Cent account on MT4 and MT5, from $15, up to 1:500 | [TMGM](https://www.tmgm.com/en/trading/cent-account) | CMA licence; suffix |

Confirm these for the chosen broker on its demo before anything else:

1. **Symbol names.** Market Watch must show the six majors exactly as `EURUSD` etc. If it shows `EURUSDc`, the system needs the mapping in section 5.
2. **Contract.** For `EURUSDc`/`EURUSD` on the cent account: minimum volume 0.01, step 0.01, tick value and contract size (read in step 7 below).
3. **Hedging.** On the demo, open two opposite positions on the same symbol. If both stay open the account is hedging; if they net, it is not and the system must not run on it.
4. **Typical spread and commission** on the six pairs during London and New York hours.

Cent accounts normally have no commission and wider spreads than raw accounts. The replay assumes raw pricing, so real cost per trade comes from R7 on the demo.

---

## 2. Demo path (do now)

### Steps that need no code change

1. **[you] Register.** Open the broker's Kenya site (for Exness, `exness.ke`) and create a profile with your email, country and a password. A demo account needs no ID or deposit ([fxsignup](https://exn.fxsignup.com/en/account/demo-account.html)).
2. **[you] Create the demo account.** In the Personal Area choose Open New Account, then Demo, then the Standard Cent type, platform **MT5**, a starting balance equal to **1,000 USC ($10)** if the form allows it, the highest leverage offered, and a trading password. Write down the **login number**, the **server name** (for example a `...MT5Trial...` server) and the trading password.
3. **[you] Log MT5 into it.** Open MetaTrader 5, then File, Open an Account. Search for the broker, select its company entry (for Exness, "Exness Technologies Ltd."), choose the server from your Personal Area, pick "Connect with an existing account", and enter login and password ([Exness guide](https://get.exness.help/hc/en-us/articles/360011514572-MT5-Trading-guide-Desktop)). The MT5 you already have works; no broker-branded build is needed. The title bar and Navigator should now show the new login.
4. **[you] Stop the engine and the EA's old state.** Before the account changes, make sure the engine is not running (`ps -Ao command | grep main.py`). Do not delete anything in the bridge folder. Old files belong to `MetaQuotes-Demo 5058284077`; the database keeps each account separately (`account_scope` such as `mt5:MetaQuotes-Demo:5058284077`).
5. **[you] Read the account type.** Terminal window, Trade tab: balance should read in USC. Right-click Market Watch, Show All, and note the names of the six majors.
6. **[you] Check hedging and spread.** Open Market Watch, add the six majors, note the spread of each. On the demo only, open a 0.01 BUY and a 0.01 SELL on one pair; confirm both stay open; close both.
7. **Read the contract** (once the EA is attached, step 9; or right-click a symbol, Specification):
   - `volume_min 0.01`, `volume_step 0.01`
   - USC account: `tick_value` for EURUSD should be about 1 (USC per tick per 1.0 cent lot) and `contract_size` about 1,000
   - pip cost of a 0.01 lot = `tick_value / tick_size x pip_size x 0.01`, which should come to about 0.1 USC ($0.001) for EURUSD
   - `filling_mode`, `stops_level`, `freeze_level`
8. **Deploy the current EA build** (merged code is `2026.11.04-1`): `scripts/mt5/compile_ea.sh --deploy`, then in MT5 remove the EA from the chart and drag `FX_Execution` back from Navigator.
9. **[you] Attach the EA** to an EURUSD H1 chart with: `ExpectedPolicyHash = f00f1f9c3e9120971378f3c1eb95d28f09d1d5e2ee97035a7f4c00a998535dc8`, `RequirePolicyHashPin = true`, `RequireDemoAccount = true`, `MagicNumber = 20261103`, Algo Trading on (green button).
10. **Run the read-only checks:**
    ```
    $PY -m validation.file_bridge_check
    $PY -m validation.readiness_preflight --strict
    ```
    Expected on a suffixed account: the symbol and bar checks fail (names not found). That is the evidence needed for section 5. Expected on a plain-name account: 43 of 43 pass.
11. **Save the evidence** to `artifacts/demo-evidence/<date>/cent-demo/`: Market Watch names, the `symbols.json` copy, the check output, spreads.

### Steps that must wait for the section 5 changes (only if the broker uses suffixes, or for any cent account)

12. Install the calendar and rates (`runbooks/demo-bringup.md`, step 3).
13. Run the EA acceptance matrix, one case at a time (`runbooks/demo-bringup.md`, step 4). The matrix payloads use plain symbol names.
14. One supervised cycle: `$PY main.py --mode demo --iterations 1`. Use the **core SRS percent-risk policy**; do not start `preserve_10` (finding 3).
15. Five-day burn-in with R7 (fill costs), R8 (drift), R9 (causes). A test wave only.

---

## 3. Live path (do not start until every gate is met)

| Gate | Status |
| --- | --- |
| A strategy is "not rejected" by the replay gate (expectancy at least +0.25R at 1.5x costs, at least 100 trades, confirming holdout) | Not met; replays not run |
| Cent demo verified (section 2, steps 1 to 11) and the section 5 changes done | Not started |
| Micro risk policy at about 1.0% to 1.5% | Not built; owner decision D3 |
| Guarded live mode and an EA build that accepts real accounts, with a lot ceiling, a trade cap and a kill switch | Not built; protected paths, second-model review and your written approval |
| SRS v1.1 pre-live gate defined (ADR 0007) | Not written |

When the gates are met:

1. **[you] Verify identity and open the live account.** On the broker's Kenya site complete the profile and verification. Expect photo ID and proof of address (the exact list for Exness Kenya is not verified; Pepperstone Kenya asks for a photo ID and a recent utility bill or bank statement).
2. **[you] Open New Account, Real, Standard Cent, MT5**, same leverage and trading password rules. Note the real login and server.
3. **[you] Fund $10** through a method the broker supports for Kenya (M-Pesa, card, bank or crypto; availability and fees not verified). Do not top up later without a new decision.
4. **[you] Log MT5 into the real server** (File, Open an Account, existing account). Repeat the section 2 checks (symbols, contract, hedging, spreads) on the live account without placing a trade.
5. Attach the live-capable EA build with `RequirePolicyHashPin = true`, the new policy hash, and `RequireDemoAccount = false`, only after the gates above.
6. Start the engine in live mode with the pilot rules agreed in advance: stop at a 30% loss ($7), review at every 20 trades with R7, R8 and R9, no manual closing of engine trades, a weekly loss-streak reset procedure, and the kill switch tested first.
7. Compare the first 20 live trades with the cent demo before any change of risk.

Do not run the engine on a laptop that sleeps with the display (see `2026-11-04-mt5-demo-and-live-account-assessment.md`). A live account belongs on a Windows VPS with the terminal and engine together.

---

## 4. What a correct cent-account bridge looks like

| Check | Expected on a $10 cent account |
| --- | --- |
| `feedback/account_snapshot.json` | `currency` is `USC` (or similar), `balance` about 1000, `account_is_demo` true on the demo |
| `market/status.json` | `login` and `server` match the new account; `terminal_connected` true; EA version `2026.11.04-1` |
| `market/symbols.json` | six symbols, `volume_min` 0.01, `volume_step` 0.01, tick values in cents |
| Lot for 1.5% risk, 25-pip stop, EURUSD | risk 15 USC / (25 x about 10 USC per lot per pip) = about 0.06 lots |

---

## 5. Changes needed before the system can trade a cent account

| # | Change | Where | Protected path |
| --- | --- | --- | --- |
| C1 | **Symbol mapping.** One configured map from the six canonical names to broker names (for example a `c` suffix), applied in the EA exports and order calls and in the engine's bridge reads; canonical names stay in the DB, reports and replay | `mt5_ea/FX_Execution.mq5`, `application/market_data_service.py`, `core/file_bridge_connection.py` | EA: yes |
| C2 | **Cent unit consistency.** Convert tick value to dollars (or the balance to account currency) in `evaluate_trade_feasibility` and `evaluate_fixed_risk_eligibility`; normalise the file bridge snapshot the way `mt5_bridge.get_account_snapshot` does; add a regression test using a USC bridge fixture | `core/mt5_bridge.py`, `core/file_bridge_connection.py` | No |
| C3 | **Micro percent policy** (about 1.0% to 1.5%) instead of the fixed-dollar `preserve_10`, so the cent path avoids fixed-dollar conversions | `config_microcapital.py`, policy hash, EA pin | Yes |
| C4 | **EA guards**: refuse a netting account; review `MaxSpreadPips` for cent spreads | `mt5_ea/FX_Execution.mq5` | Yes |
| C5 | **Guarded live mode** and a real-account EA build | `main.py`, `mt5_ea/FX_Execution.mq5`, SRS v1.1 | Yes |

C2 has no protected path and can be done in a normal development wave. C1 to C5 need a second-model review and your approval.

---

## 6. Switching back

To return to `MetaQuotes-Demo 5058284077`: stop the engine, log MT5 into that account (File, Login to Trading Account), wait until `market/status.json` shows its login, then start the engine. Evidence streams are separate per account scope, so the two accounts' histories do not mix.
