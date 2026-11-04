"""H-006 london_breakout_v1: rule branches on hand-built frames, contract on the live-bars fixture."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backtesting.unified_replay import load_bridge_bar_exports
from core.strategy import canonical_params
from tests.strategy_contract import assert_strategy_contract, run_at, session_decision_times
from tests.strategy_synthetic import POLICY, factory, frames_for, m15, regime

SID = "london_breakout_v1"
FIXTURE = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
DAY = pd.Timestamp("2026-01-13", tz="UTC")  # Tuesday, London = UTC
RANGE = (1.1005, 1.1015)                    # Asian closes; range 1.1003..1.1017 with the 2-pip wicks


def day_frames(breakout: float, *, after=(), day: pd.Timestamp = DAY, range_closes=RANGE,
               drop: pd.Timestamp | None = None, start_hour_utc: int = 7, symbol: str = "EURUSD"):
    """Bars from 00:00 UTC the day before; Asian range alternates ``range_closes``; first post-range
    bar closes inside, the second at ``breakout``, then ``after``."""
    start = day - pd.Timedelta(days=1)
    pre = int((day + pd.Timedelta(hours=start_hour_utc) - start) / pd.Timedelta(minutes=15))
    closes = [range_closes[i % 2] for i in range(pre)] + [sum(range_closes) / 2, breakout, *after]
    frame = m15(start.isoformat(), closes)
    if drop is not None:
        frame = frame.drop(index=drop)
    return frames_for(symbol, frame)


def at(day: pd.Timestamp, hhmm: str) -> pd.Timestamp:
    return day + pd.Timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[3:]))


def decide(frames, t, stub=regime(), **kw):
    decision, _, _ = run_at(factory(SID, "EURUSD", stub, **kw), frames, t)
    return decision


def test_params_defaults_are_the_card_values():
    assert canonical_params(SID) == {"entry_end_hour": 10, "max_stop_pips": 60.0, "min_stop_pips": 20.0,
                                     "range_end_hour": 7, "range_start_hour": 0, "stop_buffer_pips": 1.0}


def test_first_close_above_the_asian_range_is_a_fixed_bracket_buy():
    frames = day_frames(1.1030, after=(1.1035, 1.1040))
    d = decide(frames, at(DAY, "07:30"))
    assert d.regime_passed and d.signal is not None, d
    s = d.signal
    assert (s.direction, s.reason_code, d.reason_code) == ("BUY", "LBO_BREAKOUT_BUY", "LBO_BREAKOUT_BUY")
    assert s.stop_pips == pytest.approx(28.0)  # 1.1030 - (1.1003 - 1 pip)
    assert s.take_profit_pips == pytest.approx(62.88)  # 2.2 x (28 + 0.4) + 0.4 at a 0.8-pip spread
    assert s.risk_reward >= 2.2 and s.order_type == "MARKET"
    assert (s.tp_mode, s.be_trigger_r, s.partial_close_r, s.trailing_atr_mult) == ("FIXED", 0.0, 0.0, 0.0)
    assert s.confidence == 0.5 and s.spread_entry == pytest.approx(0.8)


def test_first_close_below_the_range_is_a_sell():
    frames = day_frames(1.0990, after=(1.0985,))
    s = decide(frames, at(DAY, "07:30")).signal
    assert s is not None and s.direction == "SELL"
    assert s.stop_pips == pytest.approx(28.0)  # (1.1017 + 1 pip) - 1.0990


@pytest.mark.parametrize("hhmm, reason", [
    ("07:00", "LBO_OUTSIDE_ENTRY_WINDOW"),  # decision at the range end: the window is (07:00, 10:00]
    ("07:15", "LBO_NO_BREAKOUT"),           # the 07:00 bar closed inside the range
    ("07:45", "LBO_ALREADY_BROKEN"),        # the 07:15 bar already closed outside
])
def test_window_and_one_attempt_per_day(hhmm, reason):
    frames = day_frames(1.1030, after=(1.1035, 1.1040))
    d = decide(frames, at(DAY, hhmm))
    assert d.signal is None and d.reason_code == reason


def test_entry_window_ends_at_10_london():
    late = [1.1010] * 9 + [1.1030]  # bars from 07:30; first close outside is the 09:45 bar -> decision 10:00
    frames = day_frames(1.1010, after=late)
    assert decide(frames, at(DAY, "10:00")).reason_code == "LBO_BREAKOUT_BUY"
    late = [1.1010] * 10 + [1.1030]  # 10:00 bar -> decision 10:15
    frames = day_frames(1.1010, after=late)
    assert decide(frames, at(DAY, "10:15")).reason_code == "LBO_OUTSIDE_ENTRY_WINDOW"


def test_missing_asian_bar_rejects_range_incomplete():
    frames = day_frames(1.1030, drop=at(DAY, "03:00"))
    assert decide(frames, at(DAY, "07:30")).reason_code == "LBO_RANGE_INCOMPLETE"


def test_dst_change_day_counts_bars_from_elapsed_time():
    # 2026-03-29: UK clocks go forward at 01:00, so 00:00-07:00 London is 00:00-06:00 UTC (24 bars).
    day = pd.Timestamp("2026-03-29", tz="UTC")
    frames = day_frames(1.1030, day=day, start_hour_utc=6)
    d = decide(frames, at(day, "06:30"))
    assert d.reason_code == "LBO_BREAKOUT_BUY", d.details


def test_breakout_against_the_h1_trend_is_rejected():
    frames = day_frames(1.1030)
    d = decide(frames, at(DAY, "07:30"), stub=regime("TRENDING_BEAR"))
    assert d.regime_passed and d.reason_code == "LBO_AGAINST_H1_TREND"
    assert decide(frames, at(DAY, "07:30"), stub=regime("TRENDING_BULL")).reason_code == "LBO_BREAKOUT_BUY"


def test_stops_outside_20_to_60_pips_are_rejected_not_padded():
    tight = day_frames(1.1018, range_closes=(1.1008, 1.1012))  # range 1.1006..1.1014
    assert decide(tight, at(DAY, "07:30")).reason_code == "LBO_STOP_TOO_TIGHT"
    wide = day_frames(1.1080, range_closes=(1.1000, 1.1060))
    assert decide(wide, at(DAY, "07:30")).reason_code == "LBO_STOP_TOO_WIDE"
    assert decide(tight, at(DAY, "07:30"), params={"min_stop_pips": 5}).reason_code == "LBO_BREAKOUT_BUY"


def test_missing_spread_fails_closed():
    frames = day_frames(1.1030)
    assert decide(frames, at(DAY, "07:30"), spread=False).reason_code == "LBO_SPREAD_UNAVAILABLE"


def test_missing_h1_data_is_a_regime_reject():
    frames = day_frames(1.1030)
    frames["EURUSD"]["H1"] = frames["EURUSD"]["H1"].iloc[:0]
    d = decide(frames, at(DAY, "07:30"), stub=None)
    assert not d.regime_passed and d.regime_reason_code == "REGIME_DATA_UNAVAILABLE" and d.signal is None


def test_contract_on_the_synthetic_day_including_the_signal():
    frames = day_frames(1.1030, after=(1.1035, 1.1040, 1.1038))
    times = [at(DAY, h) for h in ("06:45", "07:00", "07:15", "07:30", "07:45", "08:00")]
    decisions = assert_strategy_contract(factory(SID, "EURUSD", regime()), frames, times, policy=POLICY)
    assert [d.signal is not None for d in decisions] == [False, False, False, True, False, False]


@pytest.mark.parametrize("symbol", ["GBPUSD", "AUDUSD"])
def test_contract_on_the_live_fixture_with_the_real_regime(symbol):
    frames = load_bridge_bar_exports(FIXTURE, [symbol])
    times = session_decision_times(frames, symbol, last=48, step=2)
    assert_strategy_contract(factory(SID, symbol, None), frames, times, policy=POLICY)
