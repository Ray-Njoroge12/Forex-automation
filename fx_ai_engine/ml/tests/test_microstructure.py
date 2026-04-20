"""Tests for microstructure.py — spread and volume features."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.features import microstructure


class TestSpreadToPips:
    def test_eurusd_10_points_equals_1_pip(self):
        assert microstructure.spread_to_pips(10, "EURUSD") == pytest.approx(1.0)

    def test_usdjpy_10_points_equals_1_pip(self):
        assert microstructure.spread_to_pips(10, "USDJPY") == pytest.approx(1.0)

    def test_unknown_symbol_defaults(self):
        # Default divisor is 10 (points per pip)
        assert microstructure.spread_to_pips(20, "XAUUSD") == pytest.approx(2.0)

    def test_case_insensitive(self):
        assert microstructure.spread_to_pips(10, "eurusd") == pytest.approx(1.0)


class TestPriceDistanceToPips:
    def test_eurusd_one_pip(self):
        # 1 pip = 0.0001 in EURUSD
        assert microstructure.price_distance_to_pips(0.0001, "EURUSD") == pytest.approx(1.0)

    def test_usdjpy_one_pip(self):
        # 1 pip = 0.01 in USDJPY
        assert microstructure.price_distance_to_pips(0.01, "USDJPY") == pytest.approx(1.0)

    def test_eurusd_atr_14_typical(self):
        # Typical M15 ATR ~= 0.0008 → 8 pips
        assert microstructure.price_distance_to_pips(0.0008, "EURUSD") == pytest.approx(8.0)


class TestVolumeZScore:
    def test_short_series_returns_0(self):
        """Less than window+1 bars → neutral."""
        s = pd.Series([100, 200, 300])
        assert microstructure.volume_zscore(s, 20) == 0.0

    def test_constant_prior_returns_0(self):
        """Constant prior window → std=0 → returns 0.0."""
        s = pd.Series([1000] * 21)
        assert microstructure.volume_zscore(s, 20) == 0.0

    def test_spike_returns_positive(self, rng):
        """Spike relative to noisy prior window should give positive z."""
        prior = rng.normal(100, 10, size=20).tolist()
        s = pd.Series(prior + [500.0])  # current = 500, prior ~ 100 ± 10
        z = microstructure.volume_zscore(s, 20)
        assert z > 3.0

    def test_drop_returns_negative(self, rng):
        prior = rng.normal(1000, 50, size=20).tolist()
        s = pd.Series(prior + [50.0])  # huge drop
        z = microstructure.volume_zscore(s, 20)
        assert z < -3.0


class TestVolumeVsMA:
    def test_short_series_returns_1(self):
        """Less than window+1 bars → neutral 1.0."""
        s = pd.Series([100, 200])
        assert microstructure.volume_vs_ma(s, 20) == 1.0

    def test_equal_to_prior_ma(self):
        # Prior 20 all 500 → MA=500. Current 500 → 1.0
        s = pd.Series([500] * 20 + [500])
        assert microstructure.volume_vs_ma(s, 20) == pytest.approx(1.0)

    def test_double_the_prior_ma(self):
        """Current = 2 × prior window's MA."""
        s = pd.Series([500] * 20 + [1000])  # prior mean = 500
        assert microstructure.volume_vs_ma(s, 20) == pytest.approx(2.0)

    def test_zero_prior_ma_returns_1(self):
        """Prior window all zeros → MA=0 → neutral."""
        s = pd.Series([0] * 20 + [500])
        assert microstructure.volume_vs_ma(s, 20) == 1.0
