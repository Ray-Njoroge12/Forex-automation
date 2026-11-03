"""Unified replay: live parity, point-in-time feed, and execution emulation."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtesting.unified_replay import (
    Candidate, CostModel, PointInTimeFeed, SimulationConfig, ea_lot_size, generate_candidates,
    load_bridge_bar_exports, normalise_frame, simulate,
)
from config_microcapital import get_policy_config

FIXTURE = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
POLICY = get_policy_config(mode_id="core_srs", env={})
LIVE_DECISION = pd.Timestamp("2026-11-03T16:30:00Z")


def test_replay_reproduces_live_engine_decisions_at_2026_11_03_1630():
    """The live engine routed GBPUSD BUY (stop 13.60, tp 29.92) and proposed AUDUSD SELL (stop 7.93)."""
    frames = load_bridge_bar_exports(FIXTURE, ["GBPUSD", "AUDUSD"])
    # Live spreads at that moment: GBPUSD 0.0 pips, AUDUSD 0.1 pips.
    cost = CostModel(use_bar_spread=False, spread_floor_pips={"GBPUSD": 0.0, "AUDUSD": 0.1})
    gbp, _ = generate_candidates("GBPUSD", frames, POLICY, cost, decision_times=[LIVE_DECISION])
    aud, _ = generate_candidates("AUDUSD", frames, POLICY, cost, decision_times=[LIVE_DECISION])
    assert len(gbp) == 1 and len(aud) == 1
    g, a = gbp[0], aud[0]
    assert g.signal["direction"] == "BUY" and g.regime["regime"] == "TRENDING_BULL"
    assert g.signal["stop_pips"] == pytest.approx(13.60, abs=0.01)
    assert g.signal["take_profit_pips"] == pytest.approx(29.92, abs=0.01)
    assert g.signal["tp_mode"] == "TRAIL"
    assert a.signal["direction"] == "SELL" and a.regime["regime"] == "TRENDING_BEAR"
    assert a.signal["stop_pips"] == pytest.approx(7.93, abs=0.01)
    assert a.signal["take_profit_pips"] == pytest.approx(17.60, abs=0.02)


def _frame(start: str, periods: int, freq: str, prices: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return normalise_frame(pd.DataFrame({"open": prices, "high": prices + 0.0005, "low": prices - 0.0005,
                                         "close": prices, "volume": 1, "spread_points": 0}, index=idx))


def test_feed_never_serves_unclosed_bars():
    m15 = _frame("2026-01-05 00:00", 200, "15min", np.full(200, 1.1))
    h1 = _frame("2026-01-05 00:00", 60, "1h", np.full(60, 1.1))
    feed = PointInTimeFeed({"EURUSD": {"M15": m15, "H1": h1}})
    feed.set_time(pd.Timestamp("2026-01-05 10:00", tz="UTC"))
    assert feed.fetch_ohlc("EURUSD", 15, 1000).index[-1] == pd.Timestamp("2026-01-05 09:45", tz="UTC")
    assert feed.fetch_ohlc("EURUSD", 16385, 1000).index[-1] == pd.Timestamp("2026-01-05 09:00", tz="UTC")
    feed.set_time(pd.Timestamp("2026-01-05 10:14:59", tz="UTC"))
    assert feed.fetch_ohlc("EURUSD", 15, 1000).index[-1] == pd.Timestamp("2026-01-05 09:45", tz="UTC")


def test_future_bars_do_not_change_earlier_candidates():
    """Look-ahead check: altering bars after the decision leaves the decision unchanged."""
    frames = load_bridge_bar_exports(FIXTURE, ["GBPUSD"])
    cost = CostModel(use_bar_spread=False, spread_floor_pips={"GBPUSD": 0.0})
    base, _ = generate_candidates("GBPUSD", frames, POLICY, cost, decision_times=[LIVE_DECISION])
    tampered = {"GBPUSD": {tf: df.copy() for tf, df in frames["GBPUSD"].items()}}
    for df in tampered["GBPUSD"].values():
        future = df.index >= LIVE_DECISION
        df.loc[future, ["open", "high", "low", "close"]] *= 1.05
    again, _ = generate_candidates("GBPUSD", tampered, POLICY, cost, decision_times=[LIVE_DECISION])
    assert [c.signal["stop_pips"] for c in again] == [c.signal["stop_pips"] for c in base]
    assert [c.signal["direction"] for c in again] == [c.signal["direction"] for c in base]


def test_ea_lot_size_matches_live_fill():
    # Live GBPUSD trade: balance 100,033.30, risk 3.2%, stop 13.6 pips -> 23.53 lots.
    assert ea_lot_size("GBPUSD", 100_033.30, 0.032, 13.6, 1.34175) == pytest.approx(23.53)


def _candidate(t: pd.Timestamp, **signal) -> Candidate:
    base = dict(trade_id="T", symbol="EURUSD", direction="BUY", stop_pips=10.0, take_profit_pips=22.0,
                risk_reward=2.2, confidence=0.7, reason_code="TECH_CONFIRMED_BUY", timestamp_utc=t.isoformat(),
                be_trigger_r=0.0, partial_close_r=0.0, trailing_atr_mult=0.0, tp_mode="FIXED")
    base.update(signal)
    regime = dict(regime="TRENDING_BULL", trend_state="BULLISH", volatility_state="NORMAL", confidence=0.8,
                  reason_code="REGIME_TREND_BULL", timestamp_utc=t.isoformat(), atr_ratio=1.0)
    return Candidate(t, "EURUSD", base, regime)


def test_simulation_fills_next_open_and_hits_fixed_target():
    prices = np.full(400, 1.1000)
    prices[301:] = 1.1030  # rally after entry: target (+22 pips + costs) reached
    m15 = _frame("2026-01-05 07:00", 400, "15min", prices)
    m15.loc[m15.index[302], "high"] = 1.1060
    frames = {"EURUSD": {"M15": m15}}
    t = m15.index[299] + pd.Timedelta(minutes=15)  # decision at close of bar 299
    cost = CostModel(slippage_pips=0.0, commission_per_lot_usd=0.0, spread_floor_pips={"EURUSD": 1.0})
    result = simulate([_candidate(t)], frames, POLICY, cost, SimulationConfig())
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert pd.Timestamp(trade["entry_time"]) == m15.index[300]
    assert trade["entry_price"] == pytest.approx(1.1001)  # bid open + 1 pip spread
    assert trade["exit_reason"] == "TARGET"
    assert trade["r_multiple"] == pytest.approx(2.2, abs=0.01)


def test_simulation_stop_first_and_break_even_move():
    prices = np.full(400, 1.1000)
    prices[301:] = 1.1008                       # price holds above the BE level
    m15 = _frame("2026-01-05 07:00", 400, "15min", prices)
    m15.loc[m15.index[300], "high"] = 1.1012   # +1.1R favourable extreme -> BE armed
    m15.loc[m15.index[302], "low"] = 1.0990    # below BE stop (entry + 1 pip) -> exit near entry
    frames = {"EURUSD": {"M15": m15}}
    t = m15.index[299] + pd.Timedelta(minutes=15)
    cost = CostModel(slippage_pips=0.0, commission_per_lot_usd=0.0, spread_floor_pips={"EURUSD": 0.0})
    cand = _candidate(t, be_trigger_r=0.8, tp_mode="TRAIL")
    trade = simulate([cand], frames, POLICY, cost, SimulationConfig()).trades[0]
    assert trade["break_even"] is True
    assert trade["exit_reason"] == "STOP"
    assert trade["r_multiple"] == pytest.approx(0.1, abs=0.01)  # +1 pip on a 10-pip stop
