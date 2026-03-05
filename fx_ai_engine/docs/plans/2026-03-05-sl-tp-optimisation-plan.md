# SL/TP Optimisation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add structural SL snapping (Approach B) and regime-driven trade management parameters (Approach A) so Python's regime intelligence controls per-trade break-even, partial close, and trailing behaviour in the MQL5 EA.

**Architecture:** Five new optional fields are added to `TechnicalSignal` and serialised into the JSON bridge signal. `TechnicalAgent` gains two new private methods: `_detect_structural_sl()` for swing-based stop snapping and `_get_trade_management_params()` for regime→parameter mapping. The MQL5 EA stores these per-trade values in parallel arrays and applies them in `ManageOpenPositions()`.

**Tech Stack:** Python 3.11, dataclasses, pandas, pytest; MQL5 (MetaTrader 5 EA)

**Run all tests from:** `fx_ai_engine/` with `USE_MT5_MOCK=1`
**Test command:** `USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/ -q`

---

## Task 1: Extend TechnicalSignal with 5 new optional fields

**Files:**
- Modify: `core/types.py`

**Step 1: Write the failing test**

Add to `tests/test_schemas.py`:

```python
def test_technical_signal_has_trade_management_defaults() -> None:
    from core.types import TechnicalSignal
    sig = TechnicalSignal(
        trade_id="AI_test",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    assert sig.be_trigger_r == 1.0
    assert sig.partial_close_r == 1.5
    assert sig.trailing_atr_mult == 2.0
    assert sig.tp_mode == "FIXED"
    assert sig.structural_sl_pips is None
```

**Step 2: Run test to verify it fails**

```bash
cd fx_ai_engine
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_schemas.py::test_technical_signal_has_trade_management_defaults -v
```
Expected: FAIL — `AttributeError: 'TechnicalSignal' object has no attribute 'be_trigger_r'`

**Step 3: Add the 5 fields to TechnicalSignal**

In `core/types.py`, after `limit_price: float = 0.0`, add:

```python
    # Trade management parameters — passed through JSON signal to MQL5 EA
    be_trigger_r: float = 1.0          # R-multiple to move SL to break-even
    partial_close_r: float = 1.5       # R-multiple for 50% partial close (0 = off)
    trailing_atr_mult: float = 2.0     # ATR multiplier for trailing SL (0 = off)
    tp_mode: str = "FIXED"             # "FIXED" = hard TP; "TRAIL" = open-ended, trail only
    structural_sl_pips: float | None = None  # Set when structural snap occurred; None = ATR used
```

**Step 4: Run test to verify it passes**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_schemas.py::test_technical_signal_has_trade_management_defaults -v
```
Expected: PASS

**Step 5: Run full suite — verify nothing broken**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/ -q
```
Expected: all existing tests still pass

**Step 6: Commit**

```bash
git add core/types.py tests/test_schemas.py
git commit -m "feat(types): add 5 optional trade management fields to TechnicalSignal"
```

---

## Task 2: Extend schema serialisation and validation

**Files:**
- Modify: `core/schemas.py`

**Step 1: Write failing tests**

Add to `tests/test_schemas.py`:

```python
def test_signal_payload_serialises_trade_management_fields() -> None:
    from core.schemas import technical_signal_to_payload
    from core.types import TechnicalSignal
    sig = TechnicalSignal(
        trade_id="AI_test2",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_BUY",
        timestamp_utc="2026-03-05T12:00:00+00:00",
        be_trigger_r=0.8,
        partial_close_r=1.2,
        trailing_atr_mult=1.5,
        tp_mode="TRAIL",
        structural_sl_pips=11.5,
    )
    payload = technical_signal_to_payload(sig, risk_percent=0.032)
    assert payload["be_trigger_r"] == 0.8
    assert payload["partial_close_r"] == 1.2
    assert payload["trailing_atr_mult"] == 1.5
    assert payload["tp_mode"] == "TRAIL"
    assert payload["structural_sl_pips"] == 11.5


def test_signal_payload_omits_structural_sl_when_none() -> None:
    from core.schemas import technical_signal_to_payload
    from core.types import TechnicalSignal
    sig = TechnicalSignal(
        trade_id="AI_test3",
        symbol="EURUSD",
        direction="SELL",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_CONFIRMED_SELL",
        timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    payload = technical_signal_to_payload(sig, risk_percent=0.032)
    assert "structural_sl_pips" not in payload


def test_validate_signal_payload_rejects_invalid_tp_mode() -> None:
    from core.schemas import SchemaError, validate_signal_payload
    with pytest.raises(SchemaError, match="tp_mode"):
        validate_signal_payload({
            "trade_id": "x",
            "symbol": "EURUSD",
            "direction": "BUY",
            "risk_percent": 0.032,
            "stop_pips": 10.0,
            "take_profit_pips": 22.0,
            "timestamp_utc": "2026-03-05T12:00:00+00:00",
            "tp_mode": "INVALID",
        })
```

**Step 2: Run tests to verify they fail**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_schemas.py -k "trade_management or omits_structural or invalid_tp" -v
```
Expected: 3 FAILs

**Step 3: Update `technical_signal_to_payload` in `core/schemas.py`**

After the `limit_price` block (around line 107), add:

```python
    # Trade management parameters (Approach A + B)
    payload["be_trigger_r"] = float(signal.be_trigger_r)
    payload["partial_close_r"] = float(signal.partial_close_r)
    payload["trailing_atr_mult"] = float(signal.trailing_atr_mult)
    payload["tp_mode"] = signal.tp_mode
    if signal.structural_sl_pips is not None:
        payload["structural_sl_pips"] = float(signal.structural_sl_pips)
```

**Step 4: Update `validate_signal_payload` in `core/schemas.py`**

After the `order_type` validation block, add:

```python
    # Validate tp_mode if present
    if "tp_mode" in payload and payload["tp_mode"] not in {"FIXED", "TRAIL"}:
        raise SchemaError("SignalPayload tp_mode must be FIXED or TRAIL")
```

**Step 5: Run tests to verify all pass**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_schemas.py -v
```
Expected: all PASS

**Step 6: Run full suite**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/ -q
```
Expected: all pass

**Step 7: Commit**

```bash
git add core/schemas.py tests/test_schemas.py
git commit -m "feat(schemas): serialise trade management fields; validate tp_mode"
```

---

## Task 3: Add `_detect_structural_sl()` to TechnicalAgent

**Files:**
- Modify: `core/agents/technical_agent.py`
- Modify: `tests/test_agents.py`

**Step 1: Write failing tests**

Add to `tests/test_agents.py`:

```python
def _make_m15_with_swing(rows: int = 30, low_at: int = 25, swing_low: float = 1.0770) -> pd.DataFrame:
    """M15 frame with a clear swing low inserted at bar `low_at`."""
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 3, 5, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=15 * i) for i in range(rows)]
    closes = [1.0800] * rows
    opens = closes[:]
    highs = [c + 0.0005 for c in closes]
    lows = [c - 0.0003 for c in closes]
    lows[low_at] = swing_low  # insert structural low
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "tick_volume": [100] * rows, "spread": [10] * rows, "real_volume": [100] * rows},
        index=pd.DatetimeIndex(times, name="time"),
    )


def test_structural_sl_snaps_to_swing_low_for_buy() -> None:
    from core.agents.technical_agent import TechnicalAgent
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    m15 = _make_m15_with_swing(rows=30, low_at=25, swing_low=1.0770)
    current_price = 1.0800
    # ATR stop ~12 pips; structural low is 30 pips away → too wide, keep ATR
    final_stop, snapped = agent._detect_structural_sl(m15, "BUY", atr_stop_pips=12.0, current_price=current_price)
    assert final_stop == 12.0
    assert snapped is None


def test_structural_sl_snaps_when_within_window() -> None:
    from core.agents.technical_agent import TechnicalAgent
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    m15 = _make_m15_with_swing(rows=30, low_at=25, swing_low=1.0789)
    current_price = 1.0800
    # Swing low is 11 pips away; ATR stop 12 pips → ratio 0.917 → inside [0.8, 1.5] window
    final_stop, snapped = agent._detect_structural_sl(m15, "BUY", atr_stop_pips=12.0, current_price=current_price)
    assert abs(final_stop - 11.0) < 0.5   # snapped to structural
    assert snapped is not None


def test_structural_sl_keeps_atr_when_too_tight() -> None:
    from core.agents.technical_agent import TechnicalAgent
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    m15 = _make_m15_with_swing(rows=30, low_at=25, swing_low=1.0796)
    current_price = 1.0800
    # Swing low 4 pips → ratio 0.33 → below 0.8 window, too tight → keep ATR
    final_stop, snapped = agent._detect_structural_sl(m15, "BUY", atr_stop_pips=12.0, current_price=current_price)
    assert final_stop == 12.0
    assert snapped is None
```

**Step 2: Run tests to verify they fail**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_agents.py -k "structural_sl" -v
```
Expected: 3 FAILs — `AttributeError: 'TechnicalAgent' object has no attribute '_detect_structural_sl'`

**Step 3: Add `_detect_structural_sl()` to `TechnicalAgent`**

Add after `_get_atr_multiplier()` in `core/agents/technical_agent.py`:

```python
    def _detect_structural_sl(
        self,
        m15: pd.DataFrame,
        direction: str,
        atr_stop_pips: float,
        current_price: float,
    ) -> tuple[float, float | None]:
        """Snap ATR-based stop to nearest swing high/low if within [0.8×, 1.5×] window.

        Returns (final_stop_pips, structural_sl_pips_or_None).
        structural_sl_pips is None when no snap occurred (ATR stop used).
        """
        lookback = 20
        pip_value = 0.0001 if "JPY" not in self.symbol else 0.01
        window = m15.tail(lookback)

        if direction == "BUY":
            structural_level = float(window["low"].min())
            structural_pips = (current_price - structural_level) / pip_value
        else:
            structural_level = float(window["high"].max())
            structural_pips = (structural_level - current_price) / pip_value

        if structural_pips <= 0 or atr_stop_pips <= 0:
            return atr_stop_pips, None

        ratio = structural_pips / atr_stop_pips
        if 0.8 <= ratio <= 1.5:
            return round(structural_pips, 2), round(structural_pips, 2)

        return atr_stop_pips, None
```

**Step 4: Run tests to verify they pass**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_agents.py -k "structural_sl" -v
```
Expected: 3 PASSes

**Step 5: Run full suite**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/ -q
```
Expected: all pass

**Step 6: Commit**

```bash
git add core/agents/technical_agent.py tests/test_agents.py
git commit -m "feat(technical_agent): add _detect_structural_sl() with snap window [0.8x, 1.5x]"
```

---

## Task 4: Add `_get_trade_management_params()` to TechnicalAgent

**Files:**
- Modify: `core/agents/technical_agent.py`
- Modify: `tests/test_agents.py`

**Step 1: Write failing tests**

Add to `tests/test_agents.py`:

```python
def test_trade_params_trending_normal_vol() -> None:
    from core.agents.technical_agent import TechnicalAgent
    from core.types import RegimeOutput
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    regime = RegimeOutput(
        regime="TRENDING_BULL", trend_state="UP", volatility_state="NORMAL",
        confidence=0.8, reason_code="REGIME_TRENDING_BULL", timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    params = agent._get_trade_management_params(regime)
    assert params["be_trigger_r"] == 0.8
    assert params["partial_close_r"] == 1.2
    assert params["trailing_atr_mult"] == 1.5
    assert params["tp_mode"] == "TRAIL"


def test_trade_params_trending_high_vol() -> None:
    from core.agents.technical_agent import TechnicalAgent
    from core.types import RegimeOutput
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    regime = RegimeOutput(
        regime="TRENDING_BEAR", trend_state="DOWN", volatility_state="HIGH",
        confidence=0.8, reason_code="REGIME_TRENDING_BEAR", timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    params = agent._get_trade_management_params(regime)
    assert params["be_trigger_r"] == 1.2
    assert params["partial_close_r"] == 1.5
    assert params["trailing_atr_mult"] == 2.0
    assert params["tp_mode"] == "TRAIL"


def test_trade_params_ranging_disables_trail() -> None:
    from core.agents.technical_agent import TechnicalAgent
    from core.types import RegimeOutput
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    regime = RegimeOutput(
        regime="RANGING", trend_state="FLAT", volatility_state="LOW",
        confidence=0.6, reason_code="REGIME_RANGING", timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    params = agent._get_trade_management_params(regime)
    assert params["be_trigger_r"] == 1.0
    assert params["partial_close_r"] == 0.0
    assert params["trailing_atr_mult"] == 0.0
    assert params["tp_mode"] == "FIXED"
```

**Step 2: Run tests to verify they fail**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_agents.py -k "trade_params" -v
```
Expected: 3 FAILs — `AttributeError: 'TechnicalAgent' object has no attribute '_get_trade_management_params'`

**Step 3: Add `_get_trade_management_params()` to `TechnicalAgent`**

Add after `_detect_structural_sl()`:

```python
    def _get_trade_management_params(self, regime: "RegimeOutput") -> dict:
        """Map regime + volatility state to per-trade management parameters.

        Returns dict with keys: be_trigger_r, partial_close_r, trailing_atr_mult, tp_mode.
        """
        is_trending = regime.regime in {"TRENDING_BULL", "TRENDING_BEAR"}

        if not is_trending:
            # Ranging / No-Trade: fixed targets, no trailing
            return {
                "be_trigger_r": 1.0,
                "partial_close_r": 0.0,
                "trailing_atr_mult": 0.0,
                "tp_mode": "FIXED",
            }

        # Trending regime — differentiate by volatility
        if regime.volatility_state == "HIGH":
            return {
                "be_trigger_r": 1.2,
                "partial_close_r": 1.5,
                "trailing_atr_mult": 2.0,
                "tp_mode": "TRAIL",
            }
        else:  # NORMAL or LOW
            return {
                "be_trigger_r": 0.8,
                "partial_close_r": 1.2,
                "trailing_atr_mult": 1.5,
                "tp_mode": "TRAIL",
            }
```

**Step 4: Run tests to verify they pass**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_agents.py -k "trade_params" -v
```
Expected: 3 PASSes

**Step 5: Run full suite**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/ -q
```
Expected: all pass

**Step 6: Commit**

```bash
git add core/agents/technical_agent.py tests/test_agents.py
git commit -m "feat(technical_agent): add _get_trade_management_params() regime→BE/trail mapping"
```

---

## Task 5: Wire both methods into `TechnicalAgent.evaluate()`

**Files:**
- Modify: `core/agents/technical_agent.py`

**Step 1: No new test needed** — the `evaluate()` integration is covered by the existing `test_technical_agent_blocks_when_regime_not_trending` and the signal output field tests you will write here.

Add to `tests/test_agents.py`:

```python
def test_technical_agent_signal_carries_trade_management_params() -> None:
    """When evaluate() produces a signal, it must include regime-driven management params."""
    h4 = _build_ohlc_series(rows=350, drift=0.0002)
    h1 = _build_ohlc_series(rows=350, drift=0.0002)
    m15 = _build_ohlc_series(rows=350, drift=0.0002)

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        if _timeframe == 16388:   # H4
            return h4
        if _timeframe == 16385:   # H1
            return h1
        return m15

    agent = TechnicalAgent("EURUSD", fetch)
    regime = RegimeOutput(
        regime="TRENDING_BULL",
        trend_state="UP",
        volatility_state="NORMAL",
        confidence=0.8,
        reason_code="REGIME_TRENDING_BULL",
        timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    signal = agent.evaluate(regime, timeframe_m15=1, timeframe_h1=16385)
    if signal is not None:
        # If a signal was produced, it must carry trade management params
        assert signal.tp_mode in {"FIXED", "TRAIL"}
        assert signal.be_trigger_r > 0
```

**Step 2: Run test to verify it fails or is inconclusive**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_agents.py::test_technical_agent_signal_carries_trade_management_params -v
```

**Step 3: Update `evaluate()` to call both methods**

In `core/agents/technical_agent.py`, replace the block after `stop_pips` / `take_profit_pips` are computed (around lines 94–97):

**Find this section:**
```python
        pip_value = 0.0001 if "JPY" not in self.symbol else 0.01
        stop_pips = float((m15_last["atr"] * atr_multiplier) / pip_value)
        take_profit_pips = float(stop_pips * 2.2)
        if stop_pips <= 0 or take_profit_pips <= 0:
            return None
```

**Replace with:**
```python
        pip_value = 0.0001 if "JPY" not in self.symbol else 0.01
        atr_stop_pips = float((m15_last["atr"] * atr_multiplier) / pip_value)

        # Approach B: snap to structural level if within [0.8×, 1.5×] of ATR stop
        current_price = float(m15_last["close"])
        stop_pips, structural_sl_pips = self._detect_structural_sl(
            m15, direction, atr_stop_pips, current_price
        )
        take_profit_pips = float(stop_pips * 2.2)
        if stop_pips <= 0 or take_profit_pips <= 0:
            return None

        # Approach A: resolve regime-driven trade management parameters
        mgmt = self._get_trade_management_params(regime)
```

Then update the `TechnicalSignal(...)` constructor call at the end of `evaluate()` to include the new fields. Find the `return TechnicalSignal(` block and add after `rsi_slope=rsi_slope,`:

```python
            be_trigger_r=mgmt["be_trigger_r"],
            partial_close_r=mgmt["partial_close_r"],
            trailing_atr_mult=mgmt["trailing_atr_mult"],
            tp_mode=mgmt["tp_mode"],
            structural_sl_pips=structural_sl_pips,
```

**Step 4: Run full suite**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/ -q
```
Expected: all pass

**Step 5: Commit**

```bash
git add core/agents/technical_agent.py tests/test_agents.py
git commit -m "feat(technical_agent): wire structural SL snap and regime params into evaluate()"
```

---

## Task 6: Update MQL5 EA — per-position arrays and new field parsing

**Files:**
- Modify: `mt5_ea/FX_Execution.mq5`

This task has no automated tests (MQL5 runs only in MT5). Review the diff carefully before deploying.

**Step 1: Add global per-position arrays**

After the `CTrade trade;` declaration (line 16), add:

```mql5
// Per-position trade management parameters (indexed by slot 0..MAX_POS-1)
#define MAX_POS 10
ulong  g_tickets[MAX_POS];
double g_be_trigger_r[MAX_POS];
double g_partial_close_r[MAX_POS];
double g_trailing_atr_mult[MAX_POS];
bool   g_tp_mode_trail[MAX_POS];
bool   g_partial_closed[MAX_POS];

void InitPositionArrays()
{
   for(int i = 0; i < MAX_POS; i++)
   {
      g_tickets[i]         = 0;
      g_be_trigger_r[i]    = 1.0;
      g_partial_close_r[i] = 1.5;
      g_trailing_atr_mult[i] = 2.0;
      g_tp_mode_trail[i]   = false;
      g_partial_closed[i]  = false;
   }
}

int FindSlot(ulong ticket)
{
   for(int i = 0; i < MAX_POS; i++)
      if(g_tickets[i] == ticket) return i;
   return -1;
}

int AllocSlot(ulong ticket)
{
   for(int i = 0; i < MAX_POS; i++)
      if(g_tickets[i] == 0) { g_tickets[i] = ticket; return i; }
   return -1; // all slots full
}

void FreeSlot(ulong ticket)
{
   int s = FindSlot(ticket);
   if(s >= 0)
   {
      g_tickets[s]         = 0;
      g_be_trigger_r[s]    = 1.0;
      g_partial_close_r[s] = 1.5;
      g_trailing_atr_mult[s] = 2.0;
      g_tp_mode_trail[s]   = false;
      g_partial_closed[s]  = false;
   }
}
```

**Step 2: Call `InitPositionArrays()` in `OnInit()`**

Find `OnInit()` and add before `return(INIT_SUCCEEDED);`:
```mql5
   InitPositionArrays();
```

**Step 3: Free slot on position close in `OnTradeTransaction()`**

In `OnTradeTransaction()`, after `WriteExitFeedback(...)`, add:
```mql5
            FreeSlot(pos_ticket);
```

**Step 4: Parse new fields in `ProcessPendingSignal()` with safe defaults**

After the existing `ExtractJsonDouble(content, "limit_price", limitPrice);` line, add:

```mql5
   double beR         = 1.0;
   double partialR    = 1.5;
   double trailMult   = 2.0;
   string tpModeStr   = "FIXED";
   ExtractJsonDouble(content, "be_trigger_r",       beR);
   ExtractJsonDouble(content, "partial_close_r",    partialR);
   ExtractJsonDouble(content, "trailing_atr_mult",  trailMult);
   ExtractJsonString(content, "tp_mode",            tpModeStr);
   bool trailMode = (tpModeStr == "TRAIL");
```

**Step 5: Pass `tp = 0` when tp_mode is TRAIL**

In the MARKET ORDER block, replace:
```mql5
      double tp = (direction == "BUY") ? (entry + takeProfitPips * PipValue(symbol)) : (entry - takeProfitPips * PipValue(symbol));
```
With:
```mql5
      double tp = 0.0;
      if(!trailMode)
         tp = (direction == "BUY") ? (entry + takeProfitPips * PipValue(symbol)) : (entry - takeProfitPips * PipValue(symbol));
```

Apply the same pattern in the LIMIT ORDER block for `double tp = ...`.

**Step 6: Store per-position values after successful execution**

After `LogDebug("EXECUTION SUCCESS: ...")`, add:
```mql5
   ulong posTicket = trade.ResultDeal();
   if(posTicket == 0) posTicket = trade.ResultOrder();
   int slot = AllocSlot(posTicket);
   if(slot >= 0)
   {
      g_be_trigger_r[slot]     = beR;
      g_partial_close_r[slot]  = partialR;
      g_trailing_atr_mult[slot]= trailMult;
      g_tp_mode_trail[slot]    = trailMode;
      g_partial_closed[slot]   = false;
   }
```

**Step 7: Update `ManageOpenPositions()` to use per-position values**

Replace the hardcoded EA input references with slot lookups. At the top of the position loop (after the `volume` line), add:

```mql5
      int slot = FindSlot(ticket);
      double slotBE      = (slot >= 0) ? g_be_trigger_r[slot]     : BreakEvenTriggerR;
      double slotPartial = (slot >= 0) ? g_partial_close_r[slot]  : PartialCloseR;
      double slotTrail   = (slot >= 0) ? g_trailing_atr_mult[slot]: TrailingATRMultiplier;
      bool   slotClosed  = (slot >= 0) ? g_partial_closed[slot]   : false;
```

Then replace all uses of `BreakEvenTriggerR` → `slotBE`, `PartialCloseR` → `slotPartial`, `TrailingATRMultiplier` → `slotTrail`.

For the partial close guard, replace the `if(PartialCloseR > 0 && currentR >= PartialCloseR)` condition with:
```mql5
      if(slotPartial > 0 && currentR >= slotPartial && !slotClosed)
```
And after a successful partial close, add:
```mql5
            if(slot >= 0) g_partial_closed[slot] = true;
```

**Step 8: Verify EA compiles in MT5**

Open MetaEditor, load `mt5_ea/FX_Execution.mq5`, press F7 (Compile). Expected: 0 errors.

**Step 9: Commit**

```bash
git add mt5_ea/FX_Execution.mq5
git commit -m "feat(ea): per-position arrays for regime-driven BE/partial/trail; parse JSON signal fields"
```

---

## Task 7: Final integration verification

**Step 1: Run full test suite**

```bash
cd fx_ai_engine
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/ -v
```
Expected: all existing tests pass + 9 new tests pass (3 structural snap + 3 trade params + 2 schema + 1 signal integration)

**Step 2: Smoke test the pipeline**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe main.py --mode smoke
```
Expected: pipeline completes without error; log shows signal with `tp_mode` field

**Step 3: Verify SRS constants unchanged**

```bash
USE_MT5_MOCK=1 /mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe -m pytest tests/test_risk_engine.py tests/test_agents.py -v
```
Expected: all pass

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat: SL/TP optimisation complete — structural snap + regime-driven trade management"
```
