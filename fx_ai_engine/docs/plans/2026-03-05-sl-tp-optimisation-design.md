# SL/TP Optimisation Design

**Date:** 2026-03-05
**Status:** Approved
**SRS impact:** None — locked constants untouched

---

## Problem

The Python intelligence layer (regime, volatility, confidence) computes SL/TP at signal generation but has no influence over **how the trade is managed after entry**. The MQL5 EA's break-even, partial close, and trailing stop parameters are fixed EA inputs — not driven by regime or market conditions. A trending regime should trail aggressively; a ranging regime should use fixed targets. That distinction currently doesn't reach the EA.

Additionally, ATR-based stops are placed at an arbitrary distance rather than behind meaningful price structure (swing highs/lows), making them more susceptible to noise-driven stop-outs.

---

## Scope

### Approach A — Regime-Driven Trade Management Parameters

Python computes per-trade values for break-even trigger, partial close threshold, ATR trailing multiplier, and TP mode. These are serialised into the JSON signal and parsed by the EA at order placement. The EA stores them in a per-position parallel array and applies them in `ManageOpenPositions()`.

### Approach B — Structural SL Cross-Reference

Before emitting a signal, `TechnicalAgent` detects the nearest swing high/low over the last 20 M15 bars and snaps the ATR-based stop to that structural level if it falls within `[0.8×, 1.5×]` of the baseline ATR stop. The `min_rr` guard is re-applied after any snap.

### Deferred — Approach C (ML Confidence-Scaled TP)

Deferred until `signal_ranker.py` has sufficient live trade data for confidence calibration.

---

## Data Flow

```
RegimeAgent → TechnicalAgent
  [B] _detect_structural_sl()  — snap ATR stop to swing high/low
  [A] _get_trade_management_params()  — regime → BE/partial/trail/tp_mode
       ↓
TechnicalSignal (5 new optional fields)
       ↓
technical_signal_to_payload() in schemas.py
       ↓
JSON signal (bridge/pending_signals/)
       ↓
FX_Execution.mq5
  ProcessPendingSignal() — parse new fields, store in per-position arrays
  ManageOpenPositions() — use per-position values instead of EA globals
```

---

## Approach B: Structural SL Detection

**Location:** `TechnicalAgent._detect_structural_sl(m15, direction, atr_stop_pips)`

**Algorithm:**
1. Look back 20 M15 bars
2. BUY: structural level = lowest `low` in lookback window
3. SELL: structural level = highest `high` in lookback window
4. Compute `structural_pips` = distance from current price to structural level
5. Snap window: `[0.8 × atr_stop_pips, 1.5 × atr_stop_pips]`
   - If `structural_pips` within window → use `structural_pips`
   - If too tight (< 0.8×) → keep ATR stop (noise risk)
   - If too wide (> 1.5×) → keep ATR stop (R:R risk)
6. After snap: re-verify `effective_tp / effective_stop ≥ min_rr (2.2)` — reject signal if fails

**Return:** `(final_stop_pips, structural_sl_pips_or_None)`
`structural_sl_pips` is `None` when snap did not occur (ATR stop used).

---

## Approach A: Regime → Parameter Mapping

**Location:** `TechnicalAgent._get_trade_management_params(regime)`

| Regime | Volatility | `be_trigger_r` | `partial_close_r` | `trailing_atr_mult` | `tp_mode` |
|---|---|---|---|---|---|
| TRENDING_BULL/BEAR | LOW | 0.8 | 1.2 | 1.5 | TRAIL |
| TRENDING_BULL/BEAR | NORMAL | 0.8 | 1.2 | 1.5 | TRAIL |
| TRENDING_BULL/BEAR | HIGH | 1.2 | 1.5 | 2.0 | TRAIL |
| RANGING | any | 1.0 | 0.0 (off) | 0.0 (off) | FIXED |

**`tp_mode` EA behaviour:**
- `FIXED`: TP price placed at order entry, never removed
- `TRAIL`: TP price = 0 at placement (open-ended), trailing stop is the sole exit

---

## Schema Changes

### `core/types.py` — TechnicalSignal (5 new optional fields)

```python
be_trigger_r: float = 1.0
partial_close_r: float = 1.5
trailing_atr_mult: float = 2.0
tp_mode: str = "FIXED"
structural_sl_pips: float | None = None
```

### `core/schemas.py`

- `technical_signal_to_payload()`: serialise 4 EA-facing fields; omit `structural_sl_pips` if None
- `validate_signal_payload()`: if `tp_mode` present, must be `"FIXED"` or `"TRAIL"`

### `mt5_ea/FX_Execution.mq5`

**New global arrays (sized to MAX_POSITIONS = 10):**
```mql5
ulong  g_tickets[10];
double g_be_trigger_r[10];
double g_partial_close_r[10];
double g_trailing_atr_mult[10];
bool   g_tp_mode_trail[10];   // true = TRAIL, false = FIXED
bool   g_partial_closed[10];  // guard: partial close fires once
```

**`ProcessPendingSignal()`:** parse new optional fields with safe defaults:
```
be_trigger_r      default = 1.0
partial_close_r   default = 1.5
trailing_atr_mult default = 2.0
tp_mode           default = "FIXED"
```
If `tp_mode == "TRAIL"`, pass `tp = 0` to `trade.Buy()` / `trade.Sell()`.

**`ManageOpenPositions()`:** look up per-position values from arrays instead of EA global inputs.

**Array lifecycle:** populated in `ProcessPendingSignal()` on successful execution; slot cleared when position closes (detected via `OnTradeTransaction`).

---

## EA Backwards Compatibility

Old signals without new fields: EA safe defaults match current hardcoded EA inputs exactly. No behaviour change for signals generated before this update.

---

## Files Changed

| File | Change |
|---|---|
| `core/types.py` | 5 new optional fields on `TechnicalSignal` |
| `core/agents/technical_agent.py` | `_detect_structural_sl()`, `_get_trade_management_params()`, updated `evaluate()` |
| `core/schemas.py` | Serialise new fields, validate `tp_mode` |
| `mt5_ea/FX_Execution.mq5` | Per-position arrays, parse new fields, `tp_mode` branching, updated `ManageOpenPositions()` |
| `tests/test_agents.py` | Structural snap cases, regime→param mapping cases |
| `tests/test_schemas.py` | New field serialisation, `tp_mode` validation |

**Not changed:** `signal_router.py`, `portfolio_manager.py`, `hard_risk_engine.py`, `execution_feedback.py`

---

## SRS Compliance

- `min_rr = 2.2`: re-verified after every structural snap — signal rejected if violated
- `base_risk = 0.032`: untouched
- `max_trades = 2`: untouched
- All hard risk engine stops: untouched
- No new bypass paths introduced
