---
name: run-demo-validation
description: Run the SRS v1 30-day demo validation suite. Executes pytest test_validate_demo.py and validate_demo --days 30, then reports PASS/WARN/ABORT/PENDING against SRS §12.2 and §12.3 thresholds.
disable-model-invocation: true
---

# Run Demo Validation

You have invoked the `/run-demo-validation` skill. Execute the following steps exactly.

---

## Step 1 — Run Unit Tests

```bash
cd /mnt/c/Users/rayng/Desktop/Forex-automation/fx_ai_engine
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_validate_demo.py -v
```

Report the test output verbatim. If any test fails, stop here and report:
```
DEMO VALIDATION BLOCKED — Unit tests failed.
Fix the failing tests before running live demo validation.
```

---

## Step 2 — Run Demo Validation Script

```bash
cd /mnt/c/Users/rayng/Desktop/Forex-automation/fx_ai_engine
/mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m validation.validate_demo --days 30
```

Capture the full output.

---

## Step 3 — Render Verdict

Parse the output and produce this table against SRS §12.2 and §12.3 thresholds:

```
DEMO VALIDATION REPORT
════════════════════════════════════════

Period:    30-day demo window
Mode:      Mock MT5 / Live MT5 (as applicable)

METRIC TABLE
┌─────────────────────┬──────────┬──────────┬────────┐
│ Metric              │ Result   │ Required │ Status │
├─────────────────────┼──────────┼──────────┼────────┤
│ Total trades        │ XX       │ ≥ 25     │ ✓/✗    │
│ Win rate            │ XX.X%    │ ≥ 45%    │ ✓/✗    │
│ Average R           │ X.XX     │ ≥ 2.0    │ ✓/✗    │
│ Max drawdown        │ XX.X%    │ ≤ 15%    │ ✓/✗    │
└─────────────────────┴──────────┴──────────┴────────┘

VERDICT: [PASS / WARN / ABORT / PENDING]
```

### Verdict rules

| Verdict | Condition |
|---------|-----------|
| **PASS** | All 4 metrics meet required threshold |
| **WARN** | No ABORT trigger, but ≥1 metric below PASS threshold |
| **ABORT** | Drawdown > 20% OR win rate < 40% OR avg R < 1.8 |
| **PENDING** | Fewer than 25 trades — insufficient data |

### On ABORT
```
⚠️  ABORT TRIGGERED — Do NOT deploy live capital.

Abort reason: [specific metric]
SRS §12.3 requires immediate review before any further demo trading.
```

### On PASS
```
✅ DEMO VALIDATION PASSED

All SRS §12.2 pre-live thresholds met.
Next step: Schedule live deployment review per SRS §13.
```
