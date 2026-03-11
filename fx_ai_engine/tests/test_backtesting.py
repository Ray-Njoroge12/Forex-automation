from __future__ import annotations

from pathlib import Path

import pytest
import pandas as pd

pytest.importorskip("backtrader")

from backtesting.bt_runner import run_backtest
from backtesting.bt_strategy import AgentBacktestStrategy, _resample_backtest_ohlc
from backtesting.threshold_audit import _build_candidate_overrides


def test_backtest_harness_runs():
    csv_path = Path(__file__).parent / "fixtures" / "ohlc_fixture.csv"
    strategy = run_backtest(str(csv_path), "EURUSD")
    assert hasattr(strategy, "signals")
    assert hasattr(strategy, "results")
    assert hasattr(strategy, "funnel_counts")
    # Fixture is 20-row synthetic data — EMA(200) warmup prevents signal generation.
    assert isinstance(strategy.results, list)
    assert strategy.funnel_counts.get("REGIME:REJECT", 0) >= 1
    assert strategy.funnel_counts.get("TECHNICAL:SKIP", 0) >= 1


def test_backtest_harness_accepts_policy_overrides():
    csv_path = Path(__file__).parent / "fixtures" / "ohlc_fixture.csv"
    strategy = run_backtest(
        str(csv_path),
        "EURUSD",
        agent_threshold_overrides={"AGENT_THRESHOLDS": {"REGIME": {"adx_no_trade_below": 18.0}}},
    )
    assert strategy.policy["AGENT_THRESHOLDS"]["REGIME"]["adx_no_trade_below"] == 18.0


def test_threshold_audit_builds_nested_overrides():
    args = type(
        "Args",
        (),
        {
            "regime_adx_no_trade_below": 18.0,
            "regime_adx_transition_below": 22.0,
            "tech_pullback_buffer_pips": 3.5,
            "tech_buy_rsi_min": None,
            "tech_buy_rsi_max": None,
            "tech_sell_rsi_min": 34.0,
            "tech_sell_rsi_max": None,
        },
    )()
    overrides = _build_candidate_overrides(args)

    assert overrides == {
        "AGENT_THRESHOLDS": {
            "REGIME": {"adx_no_trade_below": 18.0, "adx_transition_below": 22.0},
            "TECHNICAL": {
                "pullback_buffer_pips": 3.5,
                "sell_rsi_min": 34.0,
            },
        }
    }


def test_backtest_resample_supports_h4_from_m15():
    idx = pd.date_range("2026-01-01", periods=16, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [1.1000 + i * 0.0001 for i in range(len(idx))],
            "high": [1.1003 + i * 0.0001 for i in range(len(idx))],
            "low": [1.0997 + i * 0.0001 for i in range(len(idx))],
            "close": [1.1001 + i * 0.0001 for i in range(len(idx))],
            "volume": [100.0] * len(idx),
        },
        index=idx,
    )

    h4 = _resample_backtest_ohlc(df, 16388)

    assert len(h4) == 1
    row = h4.iloc[0]
    assert float(row["open"]) == float(df.iloc[0]["open"])
    assert float(row["close"]) == float(df.iloc[-1]["close"])
    assert float(row["high"]) == float(df["high"].max())
    assert float(row["low"]) == float(df["low"].min())
    assert float(row["volume"]) == float(df["volume"].sum())


def test_backtest_fetch_ohlc_uses_current_slice_not_future_data():
    class _FakeLine:
        def __init__(self, values):
            self.values = list(values)

        def __getitem__(self, idx):
            if idx == 0:
                return self.values[2]
            if idx < 0:
                return self.values[2 + idx]
            raise IndexError(idx)

    class _FakeDateLine(_FakeLine):
        def datetime(self, _idx):
            return self.values[2]

    class _FakeData:
        def __init__(self):
            timestamps = [1735689600 + i * 900 for i in range(5)]
            self.datetime = _FakeDateLine(timestamps)
            self.open = _FakeLine([1, 2, 3, 4, 5])
            self.high = _FakeLine([2, 3, 4, 5, 6])
            self.low = _FakeLine([0, 1, 2, 3, 4])
            self.close = _FakeLine([1.5, 2.5, 3.5, 4.5, 5.5])
            self.volume = _FakeLine([10, 20, 30, 40, 50])

        def __len__(self):
            return 3

    class _FakeSelf:
        def __init__(self):
            self.p = type("P", (), {"symbol": "EURUSD"})()
            self.data = _FakeData()
            self._last_dt = None
            self._current_bar_count = 3

    fake = _FakeSelf()
    df = AgentBacktestStrategy._fetch_ohlc(fake, "EURUSD", 15, 400)

    assert list(df["open"]) == [1, 2, 3]
    assert list(df["close"]) == [1.5, 2.5, 3.5]
    assert list(df["volume"]) == [10, 20, 30]
