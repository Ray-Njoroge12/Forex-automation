---
name: srs-validate
description: SRS v1 self-check checklist. Activates automatically when Claude is about to edit files in core/risk/, core/agents/, or bridge/. Claude-only background knowledge — not user-invocable.
user-invocable: false
---

# SRS v1 Self-Check — Pre-Edit Checklist

**Trigger condition:** You are about to edit any file in `core/risk/`, `core/agents/`, or `bridge/`.

Before making any edit, mentally run through this checklist. If any item fails, STOP and report to the user before proceeding.

---

## Checklist

### Numeric Constants (10 items)

- [ ] 1. `base_risk` in `portfolio_manager.py` remains `0.032` (3.2%)
- [ ] 2. `max_trades` in `portfolio_manager.py` remains `2`
- [ ] 3. `max_exposure` in `portfolio_manager.py` remains `0.05` (5%)
- [ ] 4. `daily_loss_pct` in `hard_risk_engine.py` remains `0.08` (8%)
- [ ] 5. `weekly_loss_pct` in `hard_risk_engine.py` remains `0.15` (15%)
- [ ] 6. `drawdown_pct` in `hard_risk_engine.py` remains `0.20` (20%)
- [ ] 7. `max_consecutive_losses` in `hard_risk_engine.py` remains `3`
- [ ] 8. `min_rr` in `technical_agent.py` remains `2.2`
- [ ] 9. Regime timeframe in `regime_agent.py` remains `"H1"`
- [ ] 10. Execution timeframe in `technical_agent.py` remains `"M15"`

### Structural Invariants (5 items)

- [ ] 11. No new code path bypasses `hard_risk_engine.py` before signal routing
- [ ] 12. `signal_router.py` still uses tmp→rename atomic write pattern
- [ ] 13. `adversarial_agent.py` can still return rejection (not forced to approve)
- [ ] 14. No new environment variables that override or skip risk checks
- [ ] 15. Instruments list unchanged: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF

---

## Reporting

**If all 15 items pass:**
Report inline before proceeding with the edit:
```
SRS SELF-CHECK PASSED (15/15) — proceeding with edit.
```

**If any item fails:**
STOP immediately. Do not make the edit. Report:
```
SRS SELF-CHECK FAILED

The following items would be violated by this change:
- Item N: [description of violation]

This change requires explicit user authorisation. Please confirm you intend to modify a locked SRS v1 constraint and reference the specific section being amended.
```

**Do not proceed with any edit that fails this checklist without explicit user authorisation.**

---

## Scope

This checklist applies to edits to:
- `fx_ai_engine/core/risk/` (any file)
- `fx_ai_engine/core/agents/` (any file)
- `fx_ai_engine/bridge/` (any file)

It does NOT apply to test files, documentation, or dashboard code.
