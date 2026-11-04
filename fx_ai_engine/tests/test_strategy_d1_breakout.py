"""H-009 d1_breakout_v1 (research only): rule branches on hand-built frames, contract on the fixture."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtesting.unified_replay import load_bridge_bar_exports
from core.indicators import calculate_atr
from core.strategy import canonical_params
from tests.strategy_contract import assert_strategy_contract, run_at, session_decision_times
from tests.strategy_synthetic import POLICY, bars, factory, frames_for, regime

SID = "d1_breakout_v1"
FIXTURE = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
LAST_D1_OPEN = pd.Timestamp("2026-01-12 22:00", tz="UTC")  # broker D1 (UTC+2 server) for Tuesday 01-13
D1_CLOSE = LAST_D1_OPEN + pd.Timedelta(days=1)             # Tuesday 22:00 UTC
FIRST = pd.Timestamp("2026-01-14 07:00", tz="UTC")          # first London-session decision after it


def d1_frame(last_close: float, *, n: int = 30, last_open: pd.Timestamp = LAST_D1_OPEN) -> pd.DataFrame:
    times = pd.date_range(end=last_open, periods=n, freq="1D", tz="UTC")
    closes = [(1.1000, 1.1020)[i % 2] for i in range(n - 1)] + [last_close]
    return bars(times, closes, wick=0.0030)


def m15_frame(times: pd.DatetimeIndex, mid: float = 1.1075) -> pd.DataFrame:
    return bars(times, mid + 0.0003 * np.sin(np.arange(len(times)) / 3.0))


def week_frames(last_close: float = 1.1080, **kw):
    times = pd.date_range(D1_CLOSE - pd.Timedelta(days=2), FIRST + pd.Timedelta(hours=3), freq="15min",
                          tz="UTC", inclusive="left")
    return frames_for("EURUSD", m15_frame(times), d1=d1_frame(last_close, **kw))


def decide(frames, t, stub=regime(), **kw):
    decision, _, _ = run_at(factory(SID, "EURUSD", stub, **kw), frames, t)
    return decision


def test_params_defaults_are_the_card_values():
    assert canonical_params(SID) == {"atr_d1_period": 20, "channel_d1_bars": 20, "max_stop_pips": 300.0,
                                     "min_stop_pips": 40.0, "stop_atr_mult": 2.0}


def test_20_day_breakout_buys_at_the_first_session_decision_with_a_d1_atr_stop():
    frames = week_frames()
    d = decide(frames, FIRST)
    assert d.signal is not None, d
    s = d.signal
    assert (s.direction, s.reason_code) == ("BUY", "D1T_BREAKOUT_BUY")
    atr = float(calculate_atr(frames["EURUSD"]["D1"].tail(21), 20).iloc[-1])
    assert s.stop_pips == pytest.approx(round(2.0 * atr / 0.0001, 2))
    assert 40 <= s.stop_pips <= 300
    assert (s.tp_mode, s.be_trigger_r, s.partial_close_r, s.trailing_atr_mult) == ("FIXED", 0.0, 0.0, 0.0)


def test_20_day_breakdown_sells():
    s = decide(week_frames(0.9950 + 0.0020), FIRST).signal
    assert s is not None and s.direction == "SELL"


@pytest.mark.parametrize("t, reason", [
    (FIRST + pd.Timedelta(minutes=15), "D1T_NOT_FIRST_SESSION_DECISION"),
    (FIRST + pd.Timedelta(hours=2), "D1T_NOT_FIRST_SESSION_DECISION"),
    (FIRST - pd.Timedelta(minutes=15), "D1T_OUTSIDE_SESSION"),
])
def test_only_the_first_session_decision_after_the_d1_close_trades(t, reason):
    assert decide(week_frames(), t).reason_code == reason


def test_weekend_gap_first_decision_is_monday_london_open():
    d1 = d1_frame(1.1080, last_open=pd.Timestamp("2026-01-15 22:00", tz="UTC"))  # closes Friday 22:00
    friday = pd.date_range("2026-01-14 22:00", "2026-01-16 21:45", freq="15min", tz="UTC")
    monday = pd.date_range("2026-01-18 22:00", "2026-01-19 09:45", freq="15min", tz="UTC")
    frames = frames_for("EURUSD", m15_frame(friday.append(monday)), d1=d1)
    assert decide(frames, pd.Timestamp("2026-01-19 07:00", tz="UTC")).reason_code == "D1T_BREAKOUT_BUY"
    assert decide(frames, pd.Timestamp("2026-01-19 07:15", tz="UTC")).reason_code == "D1T_NOT_FIRST_SESSION_DECISION"


def test_no_breakout_inside_the_channel():
    assert decide(week_frames(1.1030), FIRST).reason_code == "D1T_NO_BREAKOUT"


def test_breakout_against_the_h1_trend_is_rejected():
    assert decide(week_frames(), FIRST, stub=regime("TRENDING_BEAR")).reason_code == "D1T_AGAINST_H1_TREND"
    assert decide(week_frames(), FIRST, stub=regime("TRENDING_BULL")).reason_code == "D1T_BREAKOUT_BUY"


def test_stop_range_and_params():
    assert decide(week_frames(), FIRST, params={"stop_atr_mult": 5.0}).reason_code == "D1T_STOP_TOO_WIDE"
    assert decide(week_frames(), FIRST, params={"min_stop_pips": 200}).reason_code == "D1T_STOP_TOO_TIGHT"


def test_short_d1_history_is_warmup_incomplete():
    assert decide(week_frames(n=15), FIRST).reason_code == "D1T_WARMUP_INCOMPLETE"


def test_missing_spread_fails_closed():
    assert decide(week_frames(), FIRST, spread=False).reason_code == "D1T_SPREAD_UNAVAILABLE"


def test_contract_on_synthetic_frames_including_the_signal():
    times = [FIRST + pd.Timedelta(minutes=m) for m in (-15, 0, 15, 60)]
    decisions = assert_strategy_contract(factory(SID, "EURUSD", regime()), week_frames(), times, policy=POLICY)
    assert [d.signal is not None for d in decisions] == [False, True, False, False]


@pytest.mark.parametrize("symbol", ["GBPUSD", "AUDUSD"])
def test_contract_on_the_live_fixture_with_the_real_regime(symbol):
    frames = load_bridge_bar_exports(FIXTURE, [symbol])
    times = session_decision_times(frames, symbol, last=48, step=2)
    assert_strategy_contract(factory(SID, symbol, None), frames, times, policy=POLICY)
