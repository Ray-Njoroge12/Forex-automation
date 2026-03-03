from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("backtrader")

from backtesting.bt_runner import run_backtest


def test_backtest_harness_runs():
    csv_path = Path(__file__).parent / "fixtures" / "ohlc_fixture.csv"
    strategy = run_backtest(str(csv_path), "EURUSD")
    assert hasattr(strategy, "signals")
    assert hasattr(strategy, "results")
    # Fixture is 20-row synthetic data — EMA(200) warmup prevents signal generation.
    assert isinstance(strategy.results, list)
