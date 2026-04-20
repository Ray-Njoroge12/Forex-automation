"""Shared test fixtures.

Generates synthetic OHLCV bars for indicator and feature-builder tests.
The synthetic data has known statistical properties so we can verify
indicators mathematically (not by comparing to an external TA library).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def rng() -> np.random.Generator:
    """Deterministic random generator — same results every test run."""
    return np.random.default_rng(seed=42)


def _make_bars(
    n: int,
    start_price: float,
    drift: float,
    volatility: float,
    rng: np.random.Generator,
    freq: str = "15min",
    start: str = "2023-01-02 00:00",
) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame.

    Uses geometric Brownian motion with a specified drift and volatility.
    Each bar's high/low is derived from the close with a small random
    intrabar range.
    """
    index = pd.date_range(start=start, periods=n, freq=freq, tz="UTC")
    log_returns = rng.normal(loc=drift, scale=volatility, size=n)
    log_prices = np.log(start_price) + log_returns.cumsum()
    closes = np.exp(log_prices)

    # Intrabar ranges (positive)
    ranges = np.abs(rng.normal(loc=0, scale=volatility * start_price, size=n))
    highs = closes + ranges * 0.6
    lows = closes - ranges * 0.4
    opens = np.concatenate(([start_price], closes[:-1]))

    # Ensure open/close are within [low, high]
    highs = np.maximum.reduce([highs, opens, closes])
    lows = np.minimum.reduce([lows, opens, closes])

    volumes = rng.integers(low=500, high=5000, size=n)
    spreads = rng.integers(low=5, high=25, size=n).astype("int32")

    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "spread": spreads,
        },
        index=index,
    )


@pytest.fixture
def m15_bars(rng) -> pd.DataFrame:
    """1000 bars of M15 data starting 2023-06-01.
    That's ~10 days. Chosen so that h1_bars (starting 2023-01-01) always
    has >= 200 bars of history before any M15 timestamp in the fixture.
    """
    return _make_bars(
        n=1000, start_price=1.1000, drift=0.00001,
        volatility=0.0008, rng=rng, freq="15min",
        start="2023-06-01 00:00",
    )


@pytest.fixture
def h1_bars(rng) -> pd.DataFrame:
    """5000 bars of H1 data starting 2023-01-01.
    That's ~208 days — way more than the 200-bar minimum lookback,
    with enough runway to cover any M15 timestamp in `m15_bars`.
    """
    return _make_bars(
        n=5000, start_price=1.1000, drift=0.00004,
        volatility=0.0015, rng=rng, freq="1h",
        start="2023-01-01 00:00",
    )


@pytest.fixture
def small_bars(rng) -> pd.DataFrame:
    """Small DataFrame (30 bars) for edge-case testing."""
    return _make_bars(
        n=30, start_price=1.1000, drift=0.0,
        volatility=0.001, rng=rng,
    )


@pytest.fixture
def trending_bars(rng) -> pd.DataFrame:
    """Strong uptrend — useful for testing trend indicators."""
    return _make_bars(
        n=300, start_price=1.0000, drift=0.0005,  # big positive drift
        volatility=0.0003, rng=rng,
    )
