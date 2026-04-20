"""Tests for technical indicators.

Verify mathematical correctness directly — don't rely on a reference TA
library. Uses synthetic data with known properties.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.features import indicators


# ══════════════════════════════════════════════════════════════════════
# EMA / SMA
# ══════════════════════════════════════════════════════════════════════

class TestEMA:
    def test_constant_series_gives_same_value(self):
        """EMA of a constant series = that constant (after warmup)."""
        s = pd.Series([5.0] * 50)
        e = indicators.ema(s, 10)
        # After warmup, EMA equals the input
        assert e.iloc[20:].round(6).eq(5.0).all()

    def test_before_warmup_is_nan(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        e = indicators.ema(s, 3)
        # min_periods=3 means first 2 values are NaN
        assert e.iloc[:2].isna().all()
        assert not pd.isna(e.iloc[2])

    def test_ema_formula(self):
        """Verify alpha = 2/(period+1) and adjust=False recursion.

        pandas `ewm(adjust=False)` applies the recursion from the very
        first observation:
            y[0] = x[0]
            y[t] = alpha * x[t] + (1 - alpha) * y[t-1]
        `min_periods=3` hides the first 2 values but the internal
        recursion still uses them as seed.
        """
        s = pd.Series([10.0, 12.0, 14.0, 11.0, 13.0])
        e = indicators.ema(s, 3)
        alpha = 2 / (3 + 1)  # = 0.5
        # y[0] = 10 (hidden by min_periods)
        # y[1] = 0.5 * 12 + 0.5 * 10 = 11 (hidden)
        # y[2] = 0.5 * 14 + 0.5 * 11 = 12.5  ← first visible value
        # y[3] = 0.5 * 11 + 0.5 * 12.5 = 11.75
        assert e.iloc[2] == pytest.approx(12.5)
        assert e.iloc[3] == pytest.approx(11.75)
        assert e.iloc[4] == pytest.approx(alpha * 13.0 + (1 - alpha) * 11.75)

    def test_invalid_period_raises(self):
        with pytest.raises(ValueError):
            indicators.ema(pd.Series([1.0]), 0)


class TestSMA:
    def test_basic(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        m = indicators.sma(s, 3)
        assert m.iloc[:2].isna().all()
        assert m.iloc[2] == pytest.approx(2.0)  # (1+2+3)/3
        assert m.iloc[3] == pytest.approx(3.0)  # (2+3+4)/3
        assert m.iloc[4] == pytest.approx(4.0)  # (3+4+5)/3


# ══════════════════════════════════════════════════════════════════════
# True Range / ATR
# ══════════════════════════════════════════════════════════════════════

class TestTrueRange:
    def test_hl_only_when_no_prev_close(self):
        df = pd.DataFrame({
            "high": [1.10, 1.11], "low": [1.08, 1.09], "close": [1.09, 1.10]
        })
        tr = indicators.true_range(df)
        # First bar: tr = H-L = 0.02
        assert tr.iloc[0] == pytest.approx(0.02)

    def test_gap_up(self):
        """H=1.12, L=1.11, prev_close=1.08 → tr = 1.12 - 1.08 = 0.04."""
        df = pd.DataFrame({
            "high":  [1.10, 1.12],
            "low":   [1.08, 1.11],
            "close": [1.08, 1.115],
        })
        tr = indicators.true_range(df)
        assert tr.iloc[1] == pytest.approx(0.04)

    def test_gap_down(self):
        """H=1.08, L=1.06, prev_close=1.10 → tr = 1.10 - 1.06 = 0.04."""
        df = pd.DataFrame({
            "high":  [1.11, 1.08],
            "low":   [1.09, 1.06],
            "close": [1.10, 1.07],
        })
        tr = indicators.true_range(df)
        assert tr.iloc[1] == pytest.approx(0.04)


class TestATR:
    def test_warmup_is_nan(self, m15_bars):
        a = indicators.atr(m15_bars, 14)
        # First 13 bars NaN (min_periods=14)
        assert a.iloc[:13].isna().all()
        assert not pd.isna(a.iloc[14])

    def test_atr_is_positive(self, m15_bars):
        a = indicators.atr(m15_bars, 14).dropna()
        assert (a > 0).all()

    def test_wilder_smoothing(self):
        """Hand-verify Wilder ATR on a known TR sequence."""
        # Build a DataFrame where TR is constant = 1.0 for every bar.
        n = 30
        df = pd.DataFrame({
            "open":  [100.0] * n,
            "high":  [101.0] * n,
            "low":   [100.0] * n,  # H-L = 1.0
            "close": [100.5] * n,
        })
        a = indicators.atr(df, 14)
        # If TR is constant 1.0, Wilder ATR converges to 1.0 quickly
        assert a.iloc[-1] == pytest.approx(1.0, abs=1e-9)


# ══════════════════════════════════════════════════════════════════════
# RSI
# ══════════════════════════════════════════════════════════════════════

class TestRSI:
    def test_range(self, m15_bars):
        r = indicators.rsi(m15_bars["close"], 14).dropna()
        assert (r >= 0).all() and (r <= 100).all()

    def test_flat_prices_gives_50(self):
        """RSI of a flat series is 50 (gain == loss == 0)."""
        s = pd.Series([1.0] * 50)
        r = indicators.rsi(s, 14)
        # After warmup, should be 50 by our convention
        assert r.iloc[-1] == pytest.approx(50.0)

    def test_monotonic_up_gives_100(self):
        """Strictly rising series → all gains, no losses → RSI = 100."""
        s = pd.Series(np.arange(100, dtype="float64"))
        r = indicators.rsi(s, 14)
        assert r.iloc[-1] == pytest.approx(100.0)

    def test_monotonic_down_gives_0(self):
        s = pd.Series(np.arange(100, 0, -1, dtype="float64"))
        r = indicators.rsi(s, 14)
        # Pure down → RSI approaches 0
        assert r.iloc[-1] < 1.0


# ══════════════════════════════════════════════════════════════════════
# ADX
# ══════════════════════════════════════════════════════════════════════

class TestADX:
    def test_range(self, trending_bars):
        a = indicators.adx(trending_bars, 14).dropna()
        assert (a >= 0).all() and (a <= 100).all()

    def test_warmup_is_nan(self, m15_bars):
        a = indicators.adx(m15_bars, 14)
        # First several bars should be NaN (needs period×2 for full smoothing)
        assert a.iloc[:13].isna().all()

    def test_trending_market_gives_high_adx(self, trending_bars):
        a = indicators.adx(trending_bars, 14).dropna()
        # A strong uptrend series should eventually reach ADX > 25
        assert a.iloc[-1] > 25.0, f"ADX on strong trend was only {a.iloc[-1]}"


# ══════════════════════════════════════════════════════════════════════
# MACD
# ══════════════════════════════════════════════════════════════════════

class TestMACD:
    def test_output_shape(self, m15_bars):
        macd_line, signal, hist = indicators.macd(m15_bars["close"])
        assert len(macd_line) == len(m15_bars)
        assert len(signal) == len(m15_bars)
        assert len(hist) == len(m15_bars)

    def test_histogram_is_difference(self, m15_bars):
        macd_line, signal, hist = indicators.macd(m15_bars["close"])
        # hist = macd - signal (checking both non-NaN values)
        diff = (hist - (macd_line - signal)).dropna()
        assert (diff.abs() < 1e-10).all()

    def test_invalid_periods_raise(self):
        s = pd.Series([1.0, 2.0, 3.0])
        with pytest.raises(ValueError):
            indicators.macd(s, fast=26, slow=12)  # fast > slow


# ══════════════════════════════════════════════════════════════════════
# Bollinger
# ══════════════════════════════════════════════════════════════════════

class TestBollinger:
    def test_middle_equals_sma(self, m15_bars):
        u, m, l = indicators.bollinger(m15_bars["close"], 20, 2.0)
        expected = indicators.sma(m15_bars["close"], 20)
        pd.testing.assert_series_equal(m, expected, check_names=False)

    def test_upper_greater_than_lower(self, m15_bars):
        u, _, l = indicators.bollinger(m15_bars["close"], 20, 2.0)
        diff = (u - l).dropna()
        # All non-degenerate bars: upper > lower
        assert (diff >= 0).all()
        # Most bars should have a real range (not exactly zero)
        assert (diff > 0).sum() > len(diff) * 0.9


# ══════════════════════════════════════════════════════════════════════
# Stochastic
# ══════════════════════════════════════════════════════════════════════

class TestStochastic:
    def test_range(self, m15_bars):
        k = indicators.stochastic_k(m15_bars, 14, 3).dropna()
        assert (k >= 0).all() and (k <= 100).all()


# ══════════════════════════════════════════════════════════════════════
# annotate()
# ══════════════════════════════════════════════════════════════════════

class TestAnnotate:
    def test_adds_expected_columns(self, m15_bars):
        out = indicators.annotate(m15_bars)
        required = {
            "ema_50", "ema_200",
            "atr_5", "atr_14", "atr_20",
            "rsi_14", "adx_14", "stoch_k_14",
            "macd_hist",
            "bb_upper_20", "bb_middle_20", "bb_lower_20",
            "volume_ma_20", "volume_std_20",
        }
        missing = required - set(out.columns)
        assert not missing, f"annotate() missing: {missing}"

    def test_does_not_mutate_input(self, m15_bars):
        orig_cols = set(m15_bars.columns)
        _ = indicators.annotate(m15_bars)
        assert set(m15_bars.columns) == orig_cols

    def test_preserves_original_columns(self, m15_bars):
        out = indicators.annotate(m15_bars)
        for col in m15_bars.columns:
            assert col in out.columns
            pd.testing.assert_series_equal(
                out[col], m15_bars[col], check_names=False
            )

    def test_rejects_missing_columns(self):
        bad = pd.DataFrame({"high": [1.0], "low": [0.5]})  # missing open/close
        with pytest.raises(ValueError, match="missing required columns"):
            indicators.annotate(bad)
