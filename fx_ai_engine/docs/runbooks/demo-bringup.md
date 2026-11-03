# Runbook: bring the system up on the MT5 demo account (Phase 1)

Account: MetaQuotes-Demo `5058284077` (demo, hedging). There is no funded account (ADR 0001, D1).
Run commands from `fx_ai_engine/` unless stated. `PY=../.venv/bin/python`.

Steps marked **[you]** need the MT5 window or a decision; the rest an agent can run.

## 1. Load the new EA build **[you]**

The compiled EA (`scripts/mt5/compile_ea.sh --deploy`) is already in `MQL5/Experts/`, but MT5 keeps running the old copy until the EA is re-attached.

1. In MT5, remove `FX_Execution` from the EURUSD H1 chart (right-click chart → Expert list → Remove).
2. Drag `FX_Execution` from Navigator → Expert Advisors back onto the chart.
3. In the inputs dialog check: `ExpectedPolicyHash = f00f1f9c…35dc8`, `RequirePolicyHashPin = true`, `RequireDemoAccount = true`, `MagicNumber = 20261103`. Allow Algo Trading.
4. Make sure the toolbar **Algo Trading** button is green.

Verify: `ea_debug.log` shows `OnInit: FX_Execution Started version=2026.11.03-1`, and `MQL5/Files/bridge/market/` contains `status.json`, 24 `bars_*.json`, `symbols.json`, `deals.json`, `calendar.json`.

## 2. Check the file bridge

```
$PY -m validation.file_bridge_check
```

All checks should PASS. `bars_*` checks need 350 bars (66 for D1); if a pair is short, open its chart once so MT5 downloads history, then re-run.

## 3. Market context

Calendar (blocking gate): review `MQL5/Files/bridge/market/calendar.json`, then install:

```
$PY -m validation.update_market_context --calendar "$BRIDGE_BASE_PATH/market/calendar.json"
```

Rates (soft modifier) **[you]**: review `data/source_records/market_context_2026-11-03_rates.candidate.json`. Confirm the Bank of Canada rate (2.25% vs 2.50%) and update after the Bank of England decision on 5 Nov 2026, then:

```
$PY -m validation.update_market_context --rates data/source_records/market_context_2026-11-03_rates.candidate.json
```

Verify: `$PY -m validation.readiness_preflight --strict` passes.

The calendar install covers 14 days, and the gate needs 7 days ahead. Re-export and install it at least weekly.

## 4. EA acceptance matrix (demo only)

```
$PY -m validation.ea_demo_matrix --output-dir artifacts/ea-demo-matrix --mode core_srs
```

Copy **one** case at a time into `$BRIDGE_BASE_PATH/pending_signals/`, wait for the result, and save evidence in `artifacts/demo-evidence/<date>/<case>/`: the feedback file, the matching `ea_debug.log` lines, and the MT5 Experts journal lines. Order: `malformed_schema`, `expired`, `policy_hash_mismatch`, `spread_limit`, `valid`, `position_limit`, `restart_duplicate`. Close the `valid` demo position afterwards and record its exit file.

## 5. Start the engine (dry check, then unattended)

One supervised cycle first:

```
$PY main.py --mode demo --iterations 1
```

Check the log for `File bridge connected … login=5058284077`, no `FILE_BRIDGE_*` errors, and decision-funnel rows in SQLite. Then install the watchdog agent:

```
../scripts/macos/install_launchd.sh --engine
```

Stop everything with `../scripts/macos/install_launchd.sh --uninstall`.

## 6. Burn-in exit gate (G1)

Five consecutive trading days running unattended. Every routed signal reaches a terminal state, with zero unresolved rows in `execution_feedback_conflicts`, and `python -m validation.reconciliation_inventory` shows no orphan reservations.

## Keeping the Mac awake

`com.fxai.keepawake` (installed) runs `caffeinate -i -m -s`. System sleep is prevented only on AC power, and closing the lid still sleeps the Mac unless an external display is attached.
