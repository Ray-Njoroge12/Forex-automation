# Runbook: set up a cent account in MT5 and connect it to the system (demo first, then live)

Updated 2026-11-04 after the cent build (tracks C1 to C5, integration branch `cent/integration`; ADRs 0011, 0012, 0013). Target: a $10 cent account on the MT5 terminal on this Mac, feeding the system through the file bridge. Run commands from `fx_ai_engine/` with `PY=../.venv/bin/python`. Steps marked **[you]** need a browser, the MT5 window or a personal decision. Broker terms change; confirm each item on the broker's own site.

## 0. What is built, what is verified, what is still open

| Item | Built | How it was verified | Still needs |
| --- | --- | --- | --- |
| C1 Symbol suffix (EA input `SymbolSuffix`; canonical names in every file) | Yes | EA staging compile 0 errors, 0 warnings; source-guard tests; suffixed bridge fixtures through the Python readers | Terminal test on a suffixed broker after deploy |
| C2 Cent units (Python sees US dollars; 1000 USC is $10) | Yes | USC fixtures: the 25-pip, 1.5% case is approved at about 0.06 lots (was rejected); USD output unchanged | A real cent account snapshot |
| C3 `micro_live` policy (1.5% risk, percent mode) | Yes | Hash pinned `53ff41104c05c4c6374ad59d62023575d648155ce190a507a7530c6e112f9489`; `core_srs` hash unchanged | Nothing: the owner confirmed 1.5% |
| C4 Netting refusal, spread-ceiling visibility | Yes | Source guards, compile | Terminal confirms MetaQuotes-Demo and the broker report `hedging` |
| C5 Guarded live mode (EA caps and kill switch; Python approval gate; reset command) | Yes, off by default | 1,055 tests on the merged tree; each refusal path has its own test; second-model reviews | Owner approval, SRS v1.1 pre-live gate, terminal tests |

Nothing has been deployed to the terminal and no live account exists. The EA binary in `MQL5/Experts` is still the 2026-11-03 build.

Decided on 2026-11-04: risk 1.5% per trade, spread ceiling stays 2.0 pips, and the live-gate files join the protected paths. Remaining owner items: the live caps (`max_total_loss_usd`, `max_orders_per_day`, `max_lot_per_order`), the evidence rule or waiver, approval of the SRS v1.1 pre-live addendum (`fx_ai_engine/docs/2026-11-04-srs-v1.1-pre-live-gate-proposal.md`, DRAFT), and the unproven strategy: the only strategy replayed loses 0.35R per trade, so live trading has no evidence behind it yet.

## 1. Choose the broker and confirm four facts

Candidates (not recommendations). **Correction 2026-11-05: Exness has no Standard Cent demo.** Exness states demo accounts exist for every account type except Standard Cent ([Exness help](https://get.exness.help/hc/en-us/articles/360010683800-Are-demo-accounts-available-for-all-account-types)), and its pages disagree on whether Cent runs on MT5 (an older page says MT4 only, a 2026 page says MT4 and MT5; [Exness help](https://get.exness.help/hc/en-us/articles/17537782786588-Standard-Cent-account)). Its Cent symbols carry the `c` suffix (`EURUSDc`). Exness's Kenyan entity is CMA-licensed ([exness.ke](https://www.exness.ke/standard-accounts/)); whether it offers Cent is not verified.

- **TMGM:** Cent on MT5 from $15, up to 1:500 ([TMGM](https://www.tmgm.com/en/trading/cent-account)); its FAQ says demo accounts exist for all account types ([TMGM FAQ](https://www.tmgm.com/en-au/faq)), so a cent demo is likely but not confirmed; a demo is added from the back office after identity verification and expires after 30 days ([TMGM help](https://www.tmgm.com/en/trading/help-center/demo)). Kenyan clients and CMA licence: not verified. Symbol suffix: not verified.
- **HFM:** Cent account ([HFM](https://www.hfm.com/int/en/trading-accounts/cent-account)); demo accounts on MT4 and MT5 from the myHF area ([HFM demo](https://www.hfm.com/int/en/trading-accounts/demo-account)); whether a Cent demo exists, and its symbol suffix, are not verified. It has a Kenya page (`hfm.com/ke`).
- **Rule that follows:** an account's type cannot be changed after creation (Exness: [Exness help](https://get.exness.help/hc/en-us/articles/360014140279-What-are-archived-trading-accounts-); other brokers say the same), and `MetaQuotes-Demo 5058284077` is a USD standard-contract account (0.01 lot = $0.10 per pip on EURUSD, verified from `symbols.json`). It cannot be converted. A cent demo has to be created at a broker that offers one, and some do not.

On the broker's demo confirm: (1) symbol names, and the suffix if any; (2) the cent contract (read from `symbols.json` after step 8); (3) **hedging**; (4) typical spread and commission during London and New York hours. A suffix is now supported (step 6); a netting account is refused by the EA.

## 2. Demo path

1. **[you] Register** on the broker's Kenya site. A demo needs no ID or deposit.
2. **[you] Create the demo account.** First confirm with the broker (chat or support) that a **Cent demo on MT5** exists; if the broker says no, skip to the note below. In the broker's Personal Area or back office choose Open Demo Account, account type Cent, platform **MT5**, balance 1,000 USC ($10) if allowed, highest leverage offered, and a trading password. Alternative inside MT5: File, Open an Account, pick the broker's server, "Open a demo account", and look at the **Account Type** dropdown. A cent type shows a cent currency (USC) in the read-only Currency field. If no cent type is listed, that server has no cent demo. Note the **login**, the exact **server name** and the password.
   - **If no broker offers a cent demo,** the first contact with a real cent contract is a live account. Then the live pilot itself is the cent test: the live gate, the EA caps and the $7 loss limit stay in force, and the contract checks in step 8 are done on the live account before any signal (no order is placed until the gate is satisfied).
3. **[you] Log MT5 into it:** File, Open an Account, search the broker, pick its company entry, choose your server, "Connect with an existing account", enter login and password ([Exness guide](https://get.exness.help/hc/en-us/articles/360011514572-MT5-Trading-guide-Desktop)). The MT5 you already run works.
4. **Stop the engine** (`ps -Ao command | grep main.py`) before the account changes. Do not delete bridge files; each account has its own `account_scope` in the database.
5. **[you] Read the account:** Trade tab balance in USC; Market Watch, Show All, note the six majors' names. Open two opposite 0.01 positions on one pair and confirm both stay open (hedging), then close them.
6. **Deploy the new EA build** (needs your approval; it replaces the binary the terminal loads): `scripts/mt5/compile_ea.sh --deploy`, then in MT5 remove `FX_Execution` from the chart and drag it back from Navigator.
7. **[you] Attach with these inputs:**
   - `SymbolSuffix`: the broker suffix, or empty.
   - `MaxSpreadPips`: leave at **2.0** (owner decision, ADR 0013). The lower of this and the policy's own 2.0 cap applies, so raising the input alone changes nothing. If the cent demo shows frequent `REJECTED_SPREAD`, pick another broker or account type. The value in force is shown in `status.json`.
   - `ExpectedPolicyHash`: the hash of the policy you will run, from `python -m validation.print_policy_hash --mode core_srs` or `--mode micro_live`.
   - `RequirePolicyHashPin` true, `RequireDemoAccount` true, `AllowLiveAccount` false, `LiveMaxLotPerOrder` 0, `LiveMaxOrdersPerDay` 0, `MagicNumber` 20261103. Allow Algo Trading (green button).
8. **Check `MQL5/Files/bridge/market/status.json`:** `ea_version` is `2026.11.04-2`; `symbols_ok` true and `symbol_errors` empty; `account_margin_mode` is `hedging`; `account_currency` (for example `USC`); `max_spread_pips_input`; `live_guard`. `symbols.json` shows `broker_symbol` per pair and the contract (`volume_min`, `volume_step`, `tick_value`). For a cent EURUSD expect `tick_value` about 1 (USC per tick per 1.0 cent lot) and a 0.01 lot pip cost of about 0.1 USC ($0.001).
9. **Run the read-only checks:**
   ```
   $PY -m validation.file_bridge_check
   $PY -m validation.readiness_preflight --strict
   ```
   `file_bridge_check` now reports the new fields and fails on a non-hedging account.
10. **Install the calendar and rates** (`runbooks/demo-bringup.md`, step 3).
11. **Run the EA acceptance matrix, one case at a time** (`runbooks/demo-bringup.md`, step 4, plus the 7 new cases in `manifest.json` with their `required_terminal_setup`: `suffix_resolved`, `symbol_missing`, `account_mode_netting`, `kill_switch`, `live_lot_ceiling`, `live_lot_below_minimum`, `live_daily_cap`). Save evidence under `artifacts/demo-evidence/<date>/cent-demo/`.
12. **Supervised engine run on the demo:**
    ```
    FX_ALLOW_NON_SRS_POLICY=1 $PY main.py --mode demo --policy-mode micro_live --iterations 1
    ```
    (`micro_live` is a non-SRS policy, allowed on a verified demo account only with that flag. The default `core_srs` needs no flag but risks 3.2% per trade.)
13. **Burn-in** of five trading days with R7 (fill costs), R8 (drift) and R9 (causes). A test wave only.

## 3. Live path (do not start until every gate is met)

| Gate | Status |
| --- | --- |
| A strategy passes the replay gate (expectancy at least +0.25R at 1.5x costs, at least 100 trades, confirming holdout), or the owner signs the evidence waiver | Not met; Stage D replays not run |
| Cent demo verified (section 2) and its terminal checks passed | Not started |
| Owner approves the merge of the protected-path changes and the EA deploy | Pending |
| SRS v1.1 pre-live gate approved (draft addendum written) | Pending |
| Windows VPS with terminal and engine together (this Mac sleeps with the display and runs out of memory) | Not started |

When the gates are met:

1. **[you] Verify identity and open the live account:** Real, Standard Cent, MT5. Expect photo ID and proof of address (the exact list for the chosen broker is not verified).
2. **[you] Fund $10** by a method the broker supports for Kenya (fees not verified). No top-ups later without a new decision.
3. **[you] Log MT5 into the real server** and repeat step 5 and step 8 checks without placing a trade.
4. **Attach the EA with the live inputs:** `RequireDemoAccount` false, `AllowLiveAccount` true, `LiveMaxLotPerOrder` > 0 (for a $10 cent account 0.10 is a sensible ceiling), `LiveMaxOrdersPerDay` > 0 (for example 3), `ExpectedPolicyHash` the `micro_live` hash. With any of these missing the EA rejects every signal on a real account with `REJECTED_LIVE_GUARD`.
5. **Create the approval file:** copy `fx_ai_engine/config/live_approval.example.json` to `fx_ai_engine/config/live_approval.json` (git-ignored) and replace every placeholder: your name, short expiry (days, not months), the real login and server, the `micro_live` hash, `starting_balance_usd` 10, `max_total_loss_usd` (the pilot rule is 7), `max_orders_per_day` and `max_lot_per_order` (not looser than the EA inputs), and either the `evidence` verdict path and sha256, or the `evidence_waiver` block (logged as CRITICAL on every start and every four hours).
6. **Start live:**
   ```
   export FX_LIVE_CONFIRM=I-ACCEPT-LIVE-RISK-<login>
   $PY main.py --mode live
   ```
   It refuses on a demo account, a missing or expired file, a login, server, policy or strategy mismatch, a bad confirm text, failed evidence, an old EA build, or EA caps looser than the approval. Per loop it re-checks the file, the `KILL_SWITCH` file and the loss limit.
7. **Emergency stop:** create a file named `KILL_SWITCH` in the bridge folder (next to `ea_debug.log`). New orders stop at the EA and the engine; open positions stay managed; delete the file to resume.
8. **After a loss-streak halt** (never clears itself): stop the engine, review R7, R8 and R9, then `$PY -m validation.reset_loss_streak --account-login <N> --account-server "<server>" --operator "<name>" --reason "<text>"` and type `RESET-LOSS-STREAK-<N>`.
9. Pilot rules: stop at a 30% loss ($7), review every 20 trades, do not close engine trades by hand, compare the first 20 live trades with the demo before any change of risk.

## 4. What a correct cent-account bridge looks like

| Check | Expected on a $10 cent account |
| --- | --- |
| `feedback/account_snapshot.json` | `currency` `USC`, `balance` about 1000, `account_is_demo` true on the demo (Python converts to $10) |
| `market/status.json` | `ea_version` `2026.11.04-2`, matching `login` and `server`, `symbols_ok` true, `account_margin_mode` `hedging` |
| `market/symbols.json` | six symbols, `volume_min` 0.01, `volume_step` 0.01, tick values in cents, `broker_symbol` |
| Lot for 1.5% risk, 25-pip stop, EURUSD | 15 USC / (25 pips x about 10 USC per lot per pip) = about 0.06 lots |

## 5. Known limits and follow-ups

- **Verified only by compile and source tests** (terminal check after deploy): suffixed symbols end to end, the three new feedback statuses reaching Python, the live-account path, counter persistence across restart and UTC day change, the kill-switch race, the lot-ceiling step-down.
- **Silent USD default.** If neither the status file nor the account snapshot has been read yet, the feedback reader and the research readers treat money as dollars. On a cent account that would record cents as dollars until the snapshot is read. See the second-model review in `.agents/tasks/cent-integration/review-bridge.md`.
- **Margin mode** is enforced at runtime only by the EA (`REJECTED_ACCOUNT_MODE`); `validation.file_bridge_check` reports it offline.
- **Not changed (other files):** `check_bridge_health.py` prints cents as dollars; `validate_demo.py` and `analyze_tendencies.py` do not list the three new statuses; the replay and research tools hard-code `core_srs`, so no replay verdict yet carries the `micro_live` hash. See `docs/handoff/`.
- Do not run live capital on this laptop; use a Windows VPS.

## 6. Switching back

To return to `MetaQuotes-Demo 5058284077`: stop the engine, log MT5 into it (File, Login to Trading Account), wait until `market/status.json` shows its login, then start the engine. Histories are kept per account scope. Reattach the EA with the core_srs hash and the demo inputs.
