from __future__ import annotations

import math

import pandas as pd
import pytest

from backtesting.replay_execution import QuoteBar, market_entry, protective_exit


def quote(**overrides):
    values = dict(start=pd.Timestamp("2026-01-01T10:15:00Z"), end=pd.Timestamp("2026-01-01T10:30:00Z"),
                  bid_open=1.1000, bid_high=1.1030, bid_low=1.0970, bid_close=1.1000,
                  ask_open=1.1002, ask_high=1.1032, ask_low=1.0972, ask_close=1.1002)
    return QuoteBar(**(values | overrides))


def test_market_entry_waits_for_later_quote_and_uses_correct_side():
    bar = quote()
    assert market_entry(direction="BUY", submitted_at=bar.start, quote=bar, pip_size=0.0001) is None
    before = bar.start - pd.Timedelta(seconds=1)
    buy = market_entry(direction="BUY", submitted_at=before, quote=bar, pip_size=0.0001, slippage_pips=0.5)
    sell = market_entry(direction="SELL", submitted_at=before, quote=bar, pip_size=0.0001, slippage_pips=0.5)
    assert buy.price == pytest.approx(1.10025)
    assert sell.price == pytest.approx(1.09995)
    assert buy.timestamp == bar.start


@pytest.mark.parametrize("direction,stop,target,expected", [("BUY", 1.099, 1.102, 1.0989),
                                                             ("SELL", 1.102, 1.099, 1.1021)])
def test_both_stop_and_target_hit_uses_adverse_path(direction, stop, target, expected):
    bar = quote()
    fill = protective_exit(direction=direction, active_before=bar.start - pd.Timedelta(seconds=1), quote=bar,
                           stop_price=stop, target_price=target, pip_size=0.0001, slippage_pips=1)
    assert fill.reason == "STOP" and fill.ambiguous
    assert fill.timestamp == bar.end
    assert fill.price == pytest.approx(expected)


@pytest.mark.parametrize("direction,bar,stop,target,expected", [
    ("BUY", quote(bid_open=1.095, bid_low=1.094, ask_open=1.0952, ask_low=1.0942), 1.099, 1.102, 1.0949),
    ("SELL", quote(bid_open=1.105, bid_high=1.106, ask_open=1.1052, ask_high=1.1062), 1.102, 1.099, 1.1053),
])
def test_gap_through_stop_uses_worse_open_and_slippage(direction, bar, stop, target, expected):
    fill = protective_exit(direction=direction, active_before=bar.start - pd.Timedelta(seconds=1), quote=bar,
                           stop_price=stop, target_price=target, pip_size=0.0001, slippage_pips=1)
    assert fill.reason == "STOP" and not fill.ambiguous
    assert fill.price == pytest.approx(expected)


def test_opening_target_precedes_later_intrabar_stop():
    bar = quote(bid_open=1.103, ask_open=1.1032)
    fill = protective_exit(direction="BUY", active_before=bar.start - pd.Timedelta(seconds=1), quote=bar,
                           stop_price=1.099, target_price=1.102, pip_size=0.0001)
    assert fill.reason == "TARGET" and not fill.ambiguous
    assert fill.price == 1.102


def test_protection_is_not_applied_before_it_is_active():
    bar = quote()
    assert protective_exit(direction="BUY", active_before=bar.start, quote=bar, stop_price=1.099,
                           target_price=1.102, pip_size=0.0001) is None


@pytest.mark.parametrize("update", [{"bid_open": math.nan}, {"ask_open": 1.0}, {"bid_low": 1.2},
                                    {"start": pd.Timestamp("2026-01-01")},
                                    {"end": pd.Timestamp("2026-01-01T10:15:00Z")}])
def test_bad_quote_data_is_rejected(update):
    with pytest.raises(ValueError):
        quote(**update)


def test_invalid_cost_and_stop_order_are_rejected():
    bar = quote()
    with pytest.raises(ValueError):
        market_entry(direction="BUY", submitted_at=bar.start, quote=bar, pip_size=0.0001, slippage_pips=-1)
    with pytest.raises(ValueError):
        protective_exit(direction="BUY", active_before=bar.start, quote=bar, stop_price=1.2,
                        target_price=1.1, pip_size=0.0001)
