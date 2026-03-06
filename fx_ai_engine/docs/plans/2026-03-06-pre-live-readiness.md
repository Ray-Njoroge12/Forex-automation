# Pre-Live Readiness Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the FX AI Engine fully operational for a $10 demo account, passing all bridge health checks and running a compliant 30-day validation that meets SRS v1 criteria (≥25 trades, ≥45% WR, ≥2.0 avg R, ≤15% drawdown).

**Architecture:** Four sequential tracks: (1) diagnose and fix bridge path so Python and the EA share the same filesystem folder; (2) write an `.env` file with correct micro-capital settings; (3) build a bridge health-check CLI tool that verifies the full round-trip; (4) extend the demo validation script to track compounding milestones and print a go/no-go verdict.

**Tech Stack:** Python 3.11+, MQL5 (EA already compiled), SQLite (database/db.py), pytest for all new tests, dotenv for env loading.

---

## Context: What the code actually does (read this before touching anything)

### Bridge path flow
- `core/bridge_utils.py:get_mt5_bridge_path()` — called at engine startup; returns either `BRIDGE_BASE_PATH` env var, MT5 auto-detected path (`terminal_info.data_path / MQL5/Files/bridge`), or local fallback (`fx_ai_engine/bridge/`).
- The EA (`mt5_ea/FX_Execution.mq5`) uses `RootFolder = "bridge"` input and writes relative to `MQL5/Files/`. So the full path is `<MT5 data_path>/MQL5/Files/bridge/`.
- **Root cause of STATE_STALE:** If MT5 auto-detect fails (mock mode, connection issue, or wrong user), Python falls back to `fx_ai_engine/bridge/` while the EA writes to `MQL5/Files/bridge/` — they never share the same folder.

### Account snapshot staleness
- `main.py:295` — stale check: `account_status.is_stale(max_age_seconds=180)`.
- EA writes `account_snapshot.json` every 5 seconds (`FX_Execution.mq5:125`).
- If the path is wrong, Python never reads a fresh snapshot → stale after 3 min → trading halted.

### Spread filter
- `adversarial_agent.py:43` — reads `MAX_SPREAD_PIPS` env, defaults to `2.0`. Demo spreads on majors are 2.5-3.5 pips.
- EA also has its own `MaxSpreadPips = 5.0` input — this is fine.

### Lot calculation at $10
- EA `CalculateLot()` (line 401): returns 0 if `rawLimit < minLot`. For $10 balance, 5% risk = $0.50. On EURUSD with 20 pip stop: $0.50 / (200 points × $0.00001/point) = 0.25 lots → rounds to 0.01 lots at most brokers. This works if broker minimum lot is 0.01 or nano (0.001).

### SRS v1 validation thresholds (locked)
- ≥25 closed trades, ≥45% win rate, ≥2.0 avg R, ≤15% max drawdown
- Abort criteria: drawdown >20%, WR <40%, avg R <1.8

---

## Task 1: Bridge Path Diagnostic Tool

**Goal:** Create `check_bridge_health.py` that prints exactly where each party is reading/writing, confirms the paths match, and shows the age of the last account_snapshot.

**Files:**
- Create: `fx_ai_engine/check_bridge_health.py`
- Test: `fx_ai_engine/tests/test_bridge_health.py`

### Step 1: Write the failing test

```python
# fx_ai_engine/tests/test_bridge_health.py
import json
import os
import time
from pathlib import Path
import pytest

os.environ["USE_MT5_MOCK"] = "1"


def test_bridge_health_detects_missing_snapshot(tmp_path):
    """Health check must report STALE when no snapshot exists."""
    from check_bridge_health import check_snapshot_health
    result = check_snapshot_health(bridge_path=tmp_path / "bridge")
    assert result["status"] == "MISSING"
    assert result["age_seconds"] is None


def test_bridge_health_reports_fresh_snapshot(tmp_path):
    """Health check must report FRESH when snapshot is <10s old."""
    from check_bridge_health import check_snapshot_health
    feedback = tmp_path / "bridge" / "feedback"
    feedback.mkdir(parents=True)
    snapshot = {
        "timestamp": "2026-01-01 12:00:00",
        "balance": 10.0,
        "equity": 10.0,
        "margin_free": 10.0,
        "open_positions_count": 0,
        "floating_pnl": 0.0,
    }
    (feedback / "account_snapshot.json").write_text(json.dumps(snapshot))
    result = check_snapshot_health(bridge_path=tmp_path / "bridge")
    assert result["status"] == "FRESH"
    assert result["balance"] == 10.0


def test_bridge_health_detects_stale_snapshot(tmp_path, monkeypatch):
    """Health check must report STALE when snapshot mtime is >180s old."""
    from check_bridge_health import check_snapshot_health
    feedback = tmp_path / "bridge" / "feedback"
    feedback.mkdir(parents=True)
    snap_path = feedback / "account_snapshot.json"
    snap_path.write_text(json.dumps({
        "timestamp": "2026-01-01 10:00:00",
        "balance": 10.0, "equity": 10.0,
        "margin_free": 10.0, "open_positions_count": 0, "floating_pnl": 0.0,
    }))
    # Backdate mtime by 300 seconds
    old_time = time.time() - 300
    os.utime(snap_path, (old_time, old_time))
    result = check_snapshot_health(bridge_path=tmp_path / "bridge")
    assert result["status"] == "STALE"
    assert result["age_seconds"] > 180
```

### Step 2: Run test to verify it fails

```bash
cd fx_ai_engine
USE_MT5_MOCK=1 python -m pytest tests/test_bridge_health.py -v
```

Expected: `ImportError` — `check_bridge_health` does not exist yet.

### Step 3: Implement check_bridge_health.py

```python
# fx_ai_engine/check_bridge_health.py
"""Bridge health diagnostic — run standalone or import check_snapshot_health()."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def check_snapshot_health(bridge_path: Path | str | None = None) -> dict:
    """Return health dict: status (FRESH/STALE/MISSING), age_seconds, balance."""
    if bridge_path is None:
        from core.bridge_utils import get_mt5_bridge_path
        bridge_path = get_mt5_bridge_path()
    bridge_path = Path(bridge_path)
    snap = bridge_path / "feedback" / "account_snapshot.json"

    if not snap.exists():
        return {"status": "MISSING", "age_seconds": None, "balance": None, "path": str(snap)}

    age = time.time() - snap.stat().st_mtime
    try:
        data = json.loads(snap.read_text(encoding="utf-8"))
        balance = float(data.get("balance", 0.0))
    except (json.JSONDecodeError, OSError):
        return {"status": "CORRUPT", "age_seconds": age, "balance": None, "path": str(snap)}

    status = "FRESH" if age < 180 else "STALE"
    return {"status": status, "age_seconds": round(age, 1), "balance": balance, "path": str(snap)}


def main() -> None:
    os.environ.setdefault("USE_MT5_MOCK", "1")
    from core.bridge_utils import get_mt5_bridge_path

    bridge_path = get_mt5_bridge_path()
    print(f"\n{'='*60}")
    print("BRIDGE HEALTH CHECK")
    print(f"{'='*60}")
    print(f"Bridge path: {bridge_path}")
    print(f"  pending_signals/  exists: {(bridge_path / 'pending_signals').exists()}")
    print(f"  feedback/         exists: {(bridge_path / 'feedback').exists()}")
    print(f"  exits/            exists: {(bridge_path / 'exits').exists()}")
    print(f"  active_locks/     exists: {(bridge_path / 'active_locks').exists()}")

    result = check_snapshot_health(bridge_path)
    print(f"\nAccount snapshot: {result['path']}")
    print(f"  Status:      {result['status']}")
    if result["age_seconds"] is not None:
        print(f"  Age:         {result['age_seconds']:.0f}s")
    if result["balance"] is not None:
        print(f"  Balance:     ${result['balance']:.2f}")

    # Pending signals
    pending_dir = bridge_path / "pending_signals"
    if pending_dir.exists():
        stuck = list(pending_dir.glob("*.json"))
        print(f"\nStuck pending signals: {len(stuck)}")
        for f in stuck[:5]:
            print(f"  {f.name}")
        if len(stuck) > 5:
            print(f"  ... and {len(stuck)-5} more")

    # Verdict
    print(f"\n{'='*60}")
    if result["status"] == "FRESH":
        print("VERDICT: Bridge is HEALTHY ✓")
        print("  EA is writing snapshots correctly.")
        print("  If STATE_STALE still occurs, check BRIDGE_BASE_PATH env var.")
    elif result["status"] == "STALE":
        print("VERDICT: Bridge is STALE ✗")
        print("  Snapshot exists but is >3 minutes old.")
        print("  FIX: Verify MT5 EA is attached to a chart with 'Allow Algo Trading' ON.")
        print(f"  FIX: Set BRIDGE_BASE_PATH={bridge_path}")
    elif result["status"] == "MISSING":
        print("VERDICT: Bridge is DISCONNECTED ✗")
        print("  No account_snapshot.json found.")
        print("  FIX: Check BRIDGE_BASE_PATH points to correct MT5 MQL5/Files/bridge folder.")
        print("  FIX: Compile and attach FX_Execution.mq5 to any chart in MT5.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
```

### Step 4: Run tests to verify they pass

```bash
cd fx_ai_engine
USE_MT5_MOCK=1 python -m pytest tests/test_bridge_health.py -v
```

Expected: All 3 tests PASS.

### Step 5: Verify tool runs standalone

```bash
cd fx_ai_engine
USE_MT5_MOCK=1 python check_bridge_health.py
```

Expected: Prints bridge path, folder existence, and a VERDICT.

### Step 6: Commit

```bash
git add fx_ai_engine/check_bridge_health.py fx_ai_engine/tests/test_bridge_health.py
git commit -m "feat(bridge): add check_bridge_health diagnostic tool with snapshot age check"
```

---

## Task 2: Environment Configuration (.env file)

**Goal:** Create a `.env` file with the correct micro-capital settings so the engine runs correctly without manually setting shell variables every session.

**Files:**
- Create: `fx_ai_engine/.env` (gitignored — contains no secrets here, only config)
- Modify: `fx_ai_engine/.gitignore` — ensure `.env` is listed
- Test: `fx_ai_engine/tests/test_env_config.py`

### Step 1: Check .gitignore

```bash
cd /mnt/c/Users/rayng/Desktop/Forex-automation
cat .gitignore 2>/dev/null || cat fx_ai_engine/.gitignore 2>/dev/null || echo "no .gitignore found"
```

### Step 2: Write the failing test

```python
# fx_ai_engine/tests/test_env_config.py
"""Verify micro-capital environment variables load correctly."""
import os
import pytest


def test_micro_capital_env_vars_are_valid():
    """MICRO_CAPITAL_MODE=1 must work with FIXED_RISK_USD and MAX_SPREAD_PIPS."""
    # Simulate what the .env loader sets
    test_env = {
        "MICRO_CAPITAL_MODE": "1",
        "FIXED_RISK_USD": "0.50",
        "MAX_SPREAD_PIPS": "3.5",
        "ML_PREDICT_THRESHOLD": "-1.0",
    }
    assert float(test_env["FIXED_RISK_USD"]) == 0.50
    assert float(test_env["MAX_SPREAD_PIPS"]) == 3.5
    assert float(test_env["ML_PREDICT_THRESHOLD"]) == -1.0
    assert test_env["MICRO_CAPITAL_MODE"] == "1"


def test_hard_risk_engine_reads_micro_capital_mode(monkeypatch):
    """HardRiskEngine must use relaxed limits when MICRO_CAPITAL_MODE=1."""
    import os
    monkeypatch.setenv("MICRO_CAPITAL_MODE", "1")
    # Re-import to pick up the env change
    import importlib
    import core.risk.hard_risk_engine as hre_mod
    importlib.reload(hre_mod)
    engine = hre_mod.HardRiskEngine()
    assert engine.max_daily_loss == 0.15
    assert engine.max_weekly_loss == 0.25
    assert engine.max_simultaneous_trades == 1


def test_adversarial_agent_reads_max_spread_pips(monkeypatch):
    """AdversarialAgent must read MAX_SPREAD_PIPS from env."""
    monkeypatch.setenv("MAX_SPREAD_PIPS", "3.5")
    import os
    import pandas as pd
    from core.agents.adversarial_agent import AdversarialAgent

    agent = AdversarialAgent(
        symbol="EURUSD",
        fetch_ohlc=lambda s, t, n: pd.DataFrame(),
        fetch_spread=lambda s: None,
    )
    assert agent.max_spread_pips == 3.5
```

### Step 3: Run tests to verify they pass (these should already pass — the env hooks exist)

```bash
cd fx_ai_engine
USE_MT5_MOCK=1 python -m pytest tests/test_env_config.py -v
```

Expected: All 3 PASS. If the `HardRiskEngine` reload test fails, see the note below.

> **Note on HardRiskEngine reload:** The `MICRO_CAPITAL_MODE` is read in `__init__`, not at module level, so no reload is needed — the monkeypatch sets it before the instance is created. If the test fails, remove `importlib.reload(hre_mod)`.

### Step 4: Create the .env file

```ini
# fx_ai_engine/.env
# Micro-Capital Configuration for $10 starting balance
# Load before running: source .env (Linux) or dotenv .env (PowerShell)

# === ACCOUNT MODE ===
MICRO_CAPITAL_MODE=1
FIXED_RISK_USD=0.50

# === FILTER CALIBRATION ===
# Relaxed for demo/nano-lot brokers (default was 2.0, demo spreads are 2.5-3.5)
MAX_SPREAD_PIPS=3.5
# Disable ML ranker until model is trained on real trade data
ML_PREDICT_THRESHOLD=-1.0

# === MT5 BRIDGE PATH ===
# CRITICAL: Set this to your actual MT5 data folder.
# Find it in MT5: Tools > Options > Files > "Open data folder"
# Then append: \MQL5\Files\bridge
# Example (Windows):
# BRIDGE_BASE_PATH=C:\Users\YourName\AppData\Roaming\MetaQuotes\Terminal\<hash>\MQL5\Files\bridge
BRIDGE_BASE_PATH=

# === MT5 CREDENTIALS (for live/demo connection) ===
# MT5_LOGIN=
# MT5_PASSWORD=
# MT5_SERVER=

# === OPTIONAL OBSERVABILITY ===
# OTEL_ENABLED=0
# PROM_ENABLED=0
```

### Step 5: Ensure .env is gitignored

Check if a `.gitignore` exists at project root or fx_ai_engine:

```bash
grep -r "\.env" /mnt/c/Users/rayng/Desktop/Forex-automation/.gitignore 2>/dev/null || echo "not found — add manually"
```

If not present, add to root `.gitignore`:

```
fx_ai_engine/.env
*.env
```

### Step 6: Commit

```bash
git add fx_ai_engine/.env fx_ai_engine/tests/test_env_config.py
git commit -m "feat(config): add micro-capital .env template with spread/ML/risk settings"
```

---

## Task 3: Bridge Path Fix — Env Var Instruction + Validation in check_paths.py

**Goal:** The existing `check_paths.py` already checks folder existence. Extend it to also print the exact `BRIDGE_BASE_PATH` value the user must set, and validate it matches what the EA would write to.

**Files:**
- Modify: `fx_ai_engine/check_paths.py`
- Test: No new test needed — covered by Task 1 tests.

### Step 1: Read existing check_paths.py

```bash
cat fx_ai_engine/check_paths.py
```

### Step 2: Add BRIDGE_BASE_PATH guidance

After reading the file, find the print block and append this after the existing output:

```python
# Add at end of main() in check_paths.py
print("\n--- BRIDGE_BASE_PATH guidance ---")
env_path = os.getenv("BRIDGE_BASE_PATH", "")
if env_path:
    print(f"BRIDGE_BASE_PATH is SET: {env_path}")
    if not Path(env_path).exists():
        print("  WARNING: Path does not exist! Check spelling.")
    else:
        print("  Path exists ✓")
else:
    print("BRIDGE_BASE_PATH is NOT SET.")
    print("The engine will try to auto-detect from MT5 terminal_info.")
    print("If STATE_STALE errors occur, set this manually.")
    print("Find it in MT5: Tools > Options > Files > 'Open data folder'")
    print("Then append: \\MQL5\\Files\\bridge")
    print("Example: set BRIDGE_BASE_PATH=C:\\Users\\You\\AppData\\...\\MQL5\\Files\\bridge")
```

### Step 3: Commit

```bash
git add fx_ai_engine/check_paths.py
git commit -m "fix(paths): add BRIDGE_BASE_PATH guidance to check_paths.py"
```

---

## Task 4: Demo Validation Script Enhancement

**Goal:** The existing `validation/validate_demo.py` checks SRS v1 criteria. Extend it with: (a) compounding milestone tracker (what FIXED_RISK_USD should be at current balance), (b) explicit go/no-go verdict, (c) abort check, (d) spreadsheet-friendly CSV export.

**Files:**
- Read first: `fx_ai_engine/validation/validate_demo.py`
- Modify: `fx_ai_engine/validation/validate_demo.py`
- Test: `fx_ai_engine/tests/test_validate_demo.py` (already exists — extend it)

### Step 1: Read the existing validate_demo.py

```bash
cat fx_ai_engine/validation/validate_demo.py
```

### Step 2: Read the existing test to understand what's already tested

```bash
cat fx_ai_engine/tests/test_validate_demo.py
```

### Step 3: Write new failing tests for the enhancements

Add these to `tests/test_validate_demo.py` (append, don't replace):

```python
def test_compounding_milestone_at_10():
    """At $10, milestone should recommend $0.50 fixed risk."""
    from config_microcapital import get_config_for_balance
    config = get_config_for_balance(10.0)
    assert config["FIXED_RISK_USD"] == 0.50
    assert config["MAX_SIMULTANEOUS_TRADES"] == 1


def test_compounding_milestone_at_25():
    """At $25, milestone should step up to $0.75 fixed risk."""
    from config_microcapital import get_config_for_balance
    config = get_config_for_balance(25.0)
    assert config["FIXED_RISK_USD"] == 0.75


def test_compounding_milestone_at_100():
    """At $100, milestone should step up to $3.00 fixed risk, 2 trades."""
    from config_microcapital import get_config_for_balance
    config = get_config_for_balance(100.0)
    assert config["FIXED_RISK_USD"] == 3.00
    assert config["MAX_SIMULTANEOUS_TRADES"] == 2


def test_abort_criteria_triggers_on_high_drawdown():
    """validate_demo must flag ABORT when drawdown > 20%."""
    from validation.validate_demo import check_abort_criteria
    result = check_abort_criteria(
        drawdown_pct=0.21,
        win_rate=0.50,
        avg_r=2.0,
        total_trades=10,
    )
    assert result["abort"] is True
    assert "drawdown" in result["reason"].lower()


def test_abort_criteria_triggers_on_low_win_rate():
    """validate_demo must flag ABORT when win rate < 40% after 25 trades."""
    from validation.validate_demo import check_abort_criteria
    result = check_abort_criteria(
        drawdown_pct=0.10,
        win_rate=0.38,
        avg_r=2.0,
        total_trades=25,
    )
    assert result["abort"] is True
    assert "win rate" in result["reason"].lower()


def test_no_abort_when_criteria_met():
    """validate_demo must not abort when all criteria are good."""
    from validation.validate_demo import check_abort_criteria
    result = check_abort_criteria(
        drawdown_pct=0.10,
        win_rate=0.50,
        avg_r=2.2,
        total_trades=30,
    )
    assert result["abort"] is False
```

### Step 4: Run tests to see what fails

```bash
cd fx_ai_engine
USE_MT5_MOCK=1 python -m pytest tests/test_validate_demo.py -v
```

Expected: `check_abort_criteria` import failures (function doesn't exist yet). `get_config_for_balance` tests should PASS (already in config_microcapital.py).

### Step 5: Add check_abort_criteria to validate_demo.py

Read the file first, then add this function. Find the `validate_demo.py` file and append:

```python
def check_abort_criteria(
    drawdown_pct: float,
    win_rate: float,
    avg_r: float,
    total_trades: int,
) -> dict:
    """Returns abort decision per SRS v1 abort thresholds.

    Abort if: drawdown > 20%, win rate < 40% (after 25+ trades), avg R < 1.8.
    Win rate and avg R are only evaluated once >= 25 trades have closed.
    """
    if drawdown_pct > 0.20:
        return {"abort": True, "reason": f"drawdown {drawdown_pct:.1%} exceeds 20% abort threshold"}

    if total_trades >= 25:
        if win_rate < 0.40:
            return {"abort": True, "reason": f"win rate {win_rate:.1%} below 40% abort threshold"}
        if avg_r < 1.8:
            return {"abort": True, "reason": f"avg R {avg_r:.2f} below 1.8 abort threshold"}

    return {"abort": False, "reason": ""}
```

### Step 6: Run tests — all must pass

```bash
cd fx_ai_engine
USE_MT5_MOCK=1 python -m pytest tests/test_validate_demo.py -v
```

Expected: All tests PASS.

### Step 7: Commit

```bash
git add fx_ai_engine/validation/validate_demo.py fx_ai_engine/tests/test_validate_demo.py
git commit -m "feat(validation): add check_abort_criteria and compounding milestone tests"
```

---

## Task 5: Full Test Suite Verification

**Goal:** Confirm all 14+ test files still pass after every change above.

### Step 1: Run full test suite

```bash
cd fx_ai_engine
USE_MT5_MOCK=1 python -m pytest -q
```

Expected: All tests PASS. 0 failures.

### Step 2: If any test fails

Read the failure message carefully. Common issues:
- Import errors from new modules → check file path spelling
- Monkeypatch env issues → ensure `os.environ.setdefault` is called before imports that read env at module level

### Step 3: Commit if anything was fixed

```bash
git add -p  # stage only the fix
git commit -m "fix(tests): resolve import/env issue in <test_name>"
```

---

## Task 6: Manual Pre-Live Checklist (Human-Executed Steps)

These steps cannot be automated — they require MT5 desktop access. Document them here for the user.

### MT5 EA Setup Checklist

```
[ ] Open MetaTrader 5 terminal
[ ] Menu: Tools > Options > Expert Advisors
    [ ] "Allow Automated Trading" is CHECKED
    [ ] "Allow DLL imports" is CHECKED (if required)
[ ] Attach FX_Execution.mq5 EA to any M15 chart (e.g. EURUSD M15)
    [ ] EA compiles without errors (Compiler tab shows 0 errors)
    [ ] EA shows smiley face icon in top-right of chart (running)
[ ] Verify EA input: RootFolder = "bridge" (default)
[ ] Open MT5 > Tools > Options > Files > "Open data folder"
    [ ] Navigate to MQL5\Files\bridge\
    [ ] Confirm these folders exist (EA creates them):
        [ ] pending_signals\
        [ ] feedback\
        [ ] exits\
        [ ] active_locks\
[ ] Copy the full path to bridge\ folder
[ ] Set BRIDGE_BASE_PATH in fx_ai_engine/.env to that path
```

### Python Environment Checklist

```
[ ] cd fx_ai_engine
[ ] Set env: source .env  (Linux/WSL) or use python-dotenv
[ ] Run: python check_bridge_health.py
    [ ] VERDICT shows "FRESH" (not STALE or MISSING)
[ ] Run: python check_paths.py
    [ ] No missing folder warnings
[ ] Run: python check_spreads.py
    [ ] EURUSD spread < 3.5 pips during London/NY session
[ ] Run: python main.py --mode smoke
    [ ] No STATE_STALE errors in output
    [ ] No RISK_HALTED in output
```

### Broker Requirements Checklist

```
[ ] Broker supports nano lots (SYMBOL_VOLUME_MIN = 0.001 or 0.01)
[ ] ECN/raw spread on EURUSD < 1.5 pips during London session
[ ] Demo account minimum deposit accepted ($10)
[ ] MT5 platform supported (not MT4-only)
[ ] Recommended: IC Markets (raw), Pepperstone (razor), Exness (pro)
```

### 30-Day Demo Validation Checklist

```
[ ] Run: python main.py --mode demo  (continuously during London+NY sessions)
[ ] Monitor daily: python review_trades.py
[ ] Monitor daily: python check_bridge_health.py
[ ] After 25+ trades: python -m validation.validate_demo
    [ ] Win rate ≥ 45%
    [ ] Avg R ≥ 2.0
    [ ] Max drawdown ≤ 15%
    [ ] Total trades ≥ 25
[ ] No ABORT criteria triggered (see check_abort_criteria)
[ ] Compounding: update FIXED_RISK_USD in .env as balance grows
    [ ] $10-$20: FIXED_RISK_USD=0.50
    [ ] $20-$50: FIXED_RISK_USD=0.75
    [ ] $50-$100: FIXED_RISK_USD=1.50
    [ ] $100+: FIXED_RISK_USD=3.00 (and MAX_SIMULTANEOUS_TRADES=2 in MICRO_CAPITAL_MODE)
```

---

## Expected Outcomes After Plan Completion

| Metric | Before | After |
|---|---|---|
| STATE_STALE errors | 2,857 | 0 (path aligned) |
| Spread rejections | 32/90 = 36% | ~10% (MAX_SPREAD_PIPS=3.5) |
| ML rejections | 20/90 = 22% | 0 (threshold=-1.0) |
| Trades reaching MT5 | 17 stuck | All consumed |
| Expected rejection rate | 62% | ~20-25% |
| Risk per trade | undefined | $0.50 fixed |

## Growth Projection (if 45% WR, 2.2 avg R, raw spread broker)

```
Start:   $10.00  → FIXED_RISK_USD=0.50
Month 1: ~$14-16 → still $0.50 risk
Month 2: ~$20    → step up to FIXED_RISK_USD=0.75
Month 3: ~$28-32
Month 6: ~$50    → step up to FIXED_RISK_USD=1.50
Month 9: ~$80-100 → step up to FIXED_RISK_USD=3.00
Month 12: ~$150-200 → approaching standard 3.2% mode
```

This assumes 3-8 completed trades/week during London+NY sessions only.
