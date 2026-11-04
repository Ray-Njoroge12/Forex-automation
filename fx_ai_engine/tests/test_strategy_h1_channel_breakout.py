"""H-007 h1_channel_breakout_v1: rule branches on hand-built frames, contract on the live-bars fixture."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtesting.unified_replay import load_bridge_bar_exports
from core.strategy import canonical_params
from tests.strategy_contract import assert_strategy_contract, run_at, session_decision_times
from tests.strategy_synthetic import POLICY, factory, frames_for, m15, regime

SID = "h1_channel_breakout_v1"
FIXTURE = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
START = pd.Timestamp("2026-01-12 00:00", tz="UTC")
PRE_BARS = 4 * 24 * 3 + 4 * 10  # three days and ten hours of oscillation; the next bar opens at 10:00
BREAK_OPEN = START + pd.Timedelta(minutes=15 * PRE_BARS)


def oscillation(n: int, mid: float = 1.1020, amp: float = 0.0020) -> list[float]:
    return list(mid + amp * np.sin(2 * np.pi * np.arange(n) / 32))


def channel_frames(*tail: float, amp: float = 0.0020, mid: float = 1.1020, pre: int = PRE_BARS):
    frame = m15(START.isoformat(), oscillation(pre, mid, amp) + list(tail))
    return frames_for("EURUSD", frame), frame


def decide(frames, t, stub=regime("TRENDING_BULL"), **kw):
    decision, _, _ = run_at(factory(SID, "EURUSD", stub, **kw), frames, t)
    return decision


def test_params_defaults_are_the_card_values():
    assert canonical_params(SID) == {"channel_h1_bars": 48, "max_stop_pips": 60.0, "min_stop_pips": 20.0,
                                     "stop_buffer_pips": 1.0, "stop_lookback_m15": 16}


def test_first_m15_close_through_the_48h_channel_in_a_bull_regime_is_a_buy():
    frames, frame = channel_frames(1.1050)
    t = BREAK_OPEN + pd.Timedelta(minutes=15)  # 10:15: the 10:00 bar is not in a closed H1 bar yet
    d = decide(frames, t)
    assert d.signal is not None, d
    s = d.signal
    assert (s.direction, s.reason_code) == ("BUY", "HCB_BREAKOUT_BUY")
    swing_low = float(frame["low"].iloc[-16:].min())
    assert s.stop_pips == pytest.approx(round((1.1050 - (swing_low - 0.0001)) / 0.0001, 2))
    assert 20 <= s.stop_pips <= 60
    assert (s.tp_mode, s.be_trigger_r, s.partial_close_r, s.trailing_atr_mult) == ("FIXED", 0.0, 0.0, 0.0)
    assert s.take_profit_pips >= 2.2 * s.stop_pips


def test_sell_mirrors_the_buy_in_a_bear_regime():
    frames, _ = channel_frames(1.0990)
    s = decide(frames, BREAK_OPEN + pd.Timedelta(minutes=15), stub=regime("TRENDING_BEAR")).signal
    assert s is not None and s.direction == "SELL"


def test_direction_comes_from_the_regime_only():
    frames, _ = channel_frames(1.1050)
    d = decide(frames, BREAK_OPEN + pd.Timedelta(minutes=15), stub=regime("TRENDING_BEAR"))
    assert d.signal is None and d.reason_code == "HCB_NO_BREAKOUT"


@pytest.mark.parametrize("name", ["TRANSITION", "NO_TRADE", "RANGING_LOW_VOL"])
def test_non_trending_regime_is_a_regime_reject(name):
    frames, _ = channel_frames(1.1050)
    d = decide(frames, BREAK_OPEN + pd.Timedelta(minutes=15), stub=regime(name))
    assert not d.regime_passed and d.regime_reason_code == "HCB_REGIME_NOT_TRENDING"
    assert d.reason_code == "TECH_SKIPPED_REGIME_REJECTED" and d.signal is None


def test_only_the_first_close_through_counts():
    frames, _ = channel_frames(1.1050, 1.1055)
    assert decide(frames, BREAK_OPEN + pd.Timedelta(minutes=30)).reason_code == "HCB_NO_BREAKOUT"


@pytest.mark.parametrize("name, prev_close, last_close", [
    ("TRENDING_BULL", 1.1005, 1.1035),  # previous close below the channel low, last close above the high
    ("TRENDING_BEAR", 1.1035, 1.1005),  # previous close above the channel high, last close below the low
])
def test_previous_close_outside_the_opposite_boundary_is_no_breakout(name, prev_close, last_close):
    frames, frame = channel_frames(prev_close, last_close, amp=0.0005)
    channel = frame.iloc[:PRE_BARS].tail(4 * 48)
    assert not channel["low"].min() <= prev_close <= channel["high"].max()
    swing = frame.tail(16)
    stop = (last_close - swing["low"].min() if name == "TRENDING_BULL" else swing["high"].max() - last_close) / 0.0001 + 1
    assert 20 <= stop <= 60  # the stop range would pass, so only the trigger rule rejects
    d = decide(frames, BREAK_OPEN + pd.Timedelta(minutes=30), stub=regime(name))
    assert d.signal is None and d.reason_code == "HCB_NO_BREAKOUT"


def test_bar_closing_on_the_hour_is_inside_the_closed_channel():
    frames, _ = channel_frames(*oscillation(3), 1.1050)  # breakout bar opens 10:45, decision 11:00
    d = decide(frames, BREAK_OPEN + pd.Timedelta(hours=1))
    assert d.reason_code == "HCB_NO_BREAKOUT"


def test_stop_range_is_enforced():
    small, _ = channel_frames(1.1030, amp=0.0005)
    assert decide(small, BREAK_OPEN + pd.Timedelta(minutes=15)).reason_code == "HCB_STOP_TOO_TIGHT"
    big, _ = channel_frames(1.1070, amp=0.0040)
    assert decide(big, BREAK_OPEN + pd.Timedelta(minutes=15)).reason_code == "HCB_STOP_TOO_WIDE"


def test_short_h1_history_is_warmup_incomplete():
    frames, _ = channel_frames(1.1050, pre=4 * 40)  # 40 H1 bars < 48
    t = START + pd.Timedelta(minutes=15 * (4 * 40 + 1))
    assert decide(frames, t).reason_code == "HCB_WARMUP_INCOMPLETE"


def test_missing_spread_fails_closed():
    frames, _ = channel_frames(1.1050)
    d = decide(frames, BREAK_OPEN + pd.Timedelta(minutes=15), spread=False)
    assert d.reason_code == "HCB_SPREAD_UNAVAILABLE"


def test_contract_on_synthetic_frames_including_the_signal():
    frames, _ = channel_frames(1.1050, 1.1052, 1.1049)
    times = [BREAK_OPEN + pd.Timedelta(minutes=m) for m in (-15, 0, 15, 30, 45)]
    decisions = assert_strategy_contract(factory(SID, "EURUSD", regime("TRENDING_BULL")), frames, times,
                                         policy=POLICY)
    assert [d.signal is not None for d in decisions] == [False, False, True, False, False]


@pytest.mark.parametrize("symbol", ["GBPUSD", "AUDUSD"])
def test_contract_on_the_live_fixture_with_the_real_regime(symbol):
    frames = load_bridge_bar_exports(FIXTURE, [symbol])
    times = session_decision_times(frames, symbol, last=48, step=2)
    decisions = assert_strategy_contract(factory(SID, symbol, None), frames, times, policy=POLICY)
    assert any(d.regime_passed for d in decisions)  # the fixture trends on both pairs at the end
