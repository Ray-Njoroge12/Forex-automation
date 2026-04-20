"""Tests for FeatureBuilder — the integration piece."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.features import FEATURE_ORDER, FEATURE_SCHEMA_VERSION, FeatureBuilder, FeatureVector


class TestFeatureBuilder:
    def test_builds_exactly_30_features(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        t = m15_bars.index[-1]
        fv = fb.build(t, regime_label="TREND_UP")
        assert fv.values.shape == (30,)
        assert len(fv.names) == 30
        assert fv.names == FEATURE_ORDER

    def test_output_is_float32(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        fv = fb.build(m15_bars.index[-1], regime_label="RANGE")
        assert fv.values.dtype == np.float32

    def test_all_values_finite(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        fv = fb.build(m15_bars.index[-1], regime_label="TREND_UP")
        assert np.all(np.isfinite(fv.values))

    def test_schema_version_stamped(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        fv = fb.build(m15_bars.index[-1])
        assert fv.schema_version == FEATURE_SCHEMA_VERSION

    def test_timestamp_matches(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        t = m15_bars.index[500]
        fv = fb.build(t)
        assert fv.timestamp == t

    def test_as_dict_round_trip(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        fv = fb.build(m15_bars.index[-1])
        d = fv.as_dict()
        assert set(d.keys()) == set(FEATURE_ORDER)
        # Values should match
        for i, name in enumerate(FEATURE_ORDER):
            assert d[name] == pytest.approx(float(fv.values[i]))

    def test_insufficient_m15_history_raises(self, m15_bars, h1_bars):
        # Only first 10 M15 bars available
        short_m15 = m15_bars.iloc[:10]
        fb = FeatureBuilder(short_m15, h1_bars, "EURUSD")
        with pytest.raises(ValueError, match="Insufficient M15 history"):
            fb.build(short_m15.index[-1])

    def test_insufficient_h1_history_raises(self, m15_bars, h1_bars):
        short_h1 = h1_bars.iloc[:50]
        fb = FeatureBuilder(m15_bars, short_h1, "EURUSD")
        with pytest.raises(ValueError, match="Insufficient H1 history"):
            fb.build(m15_bars.index[-1])

    def test_non_datetime_index_raises(self):
        bad = pd.DataFrame({
            "open": [1.0], "high": [1.1], "low": [0.9],
            "close": [1.0], "volume": [100],
        })  # RangeIndex, not DatetimeIndex
        with pytest.raises(TypeError, match="DatetimeIndex"):
            FeatureBuilder(bad, bad, "EURUSD")

    def test_empty_dataframe_raises(self, h1_bars):
        empty = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
            index=pd.DatetimeIndex([], tz="UTC"),
        )
        with pytest.raises(ValueError, match="not be empty"):
            FeatureBuilder(empty, h1_bars, "EURUSD")

    def test_no_look_ahead(self, m15_bars, h1_bars):
        """Feature vector at time t should not depend on data after t."""
        full_fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        truncated_fb = FeatureBuilder(
            m15_bars.loc[:m15_bars.index[500]],
            h1_bars.loc[:m15_bars.index[500]],
            "EURUSD",
        )
        t = m15_bars.index[500]
        fv_full = full_fb.build(t)
        fv_trunc = truncated_fb.build(t)
        # Values must be identical
        np.testing.assert_allclose(
            fv_full.values, fv_trunc.values, rtol=1e-5,
            err_msg="Look-ahead detected: truncated vs full differ",
        )

    def test_regime_encoding_reflected(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        t = m15_bars.index[-1]

        fv_range = fb.build(t, regime_label="RANGE")
        fv_trend = fb.build(t, regime_label="TREND_UP")
        fv_none = fb.build(t, regime_label=None)

        i = FEATURE_ORDER.index("regime_encoded")
        assert fv_range.values[i] == 0.0
        assert fv_trend.values[i] == 2.0
        assert fv_none.values[i] == 0.0  # UNKNOWN → 0

    def test_feature_vector_shape_enforced(self):
        """FeatureVector raises on wrong shape."""
        bad_vals = np.zeros(29, dtype=np.float32)
        with pytest.raises(ValueError, match="shape"):
            FeatureVector(
                values=bad_vals,
                names=FEATURE_ORDER,
                timestamp=pd.Timestamp("2024-01-01", tz="UTC"),
                schema_version=FEATURE_SCHEMA_VERSION,
            )

    def test_feature_vector_dtype_enforced(self):
        """FeatureVector raises on wrong dtype."""
        vals64 = np.zeros(30, dtype=np.float64)
        with pytest.raises(ValueError, match="float32"):
            FeatureVector(
                values=vals64,
                names=FEATURE_ORDER,
                timestamp=pd.Timestamp("2024-01-01", tz="UTC"),
                schema_version=FEATURE_SCHEMA_VERSION,
            )


class TestFeatureContract:
    def test_feature_order_is_30(self):
        assert len(FEATURE_ORDER) == 30

    def test_feature_order_no_duplicates(self):
        assert len(set(FEATURE_ORDER)) == len(FEATURE_ORDER)

    def test_schema_version_is_string(self):
        assert isinstance(FEATURE_SCHEMA_VERSION, str)
        assert len(FEATURE_SCHEMA_VERSION) > 0


class TestFeatureParity:
    """The MOST IMPORTANT test: building features twice on the same data
    must produce bit-identical output. This is the guarantee that
    training and live inference compute the same thing."""

    def test_identical_runs_produce_identical_output(self, m15_bars, h1_bars):
        fb1 = FeatureBuilder(m15_bars.copy(), h1_bars.copy(), "EURUSD")
        fb2 = FeatureBuilder(m15_bars.copy(), h1_bars.copy(), "EURUSD")

        t = m15_bars.index[500]
        fv1 = fb1.build(t, regime_label="TREND_UP")
        fv2 = fb2.build(t, regime_label="TREND_UP")

        # Exact byte equality
        np.testing.assert_array_equal(fv1.values, fv2.values)

    def test_multiple_timestamps_all_produce_valid_output(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        # Sample 10 timestamps across the M15 history (avoiding early warmup)
        sample_indices = range(250, len(m15_bars), 75)
        for i in sample_indices:
            t = m15_bars.index[i]
            fv = fb.build(t, regime_label="TREND_UP")
            assert fv.values.shape == (30,)
            assert np.all(np.isfinite(fv.values))


class TestFeatureRanges:
    """Sanity checks on feature values — no indicator should blow up."""

    def test_rsi_in_range(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        fv = fb.build(m15_bars.index[-1])
        rsi_i = FEATURE_ORDER.index("rsi_14")
        assert 0 <= fv.values[rsi_i] <= 100

    def test_adx_in_range(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        fv = fb.build(m15_bars.index[-1])
        adx_i = FEATURE_ORDER.index("adx_14")
        assert 0 <= fv.values[adx_i] <= 100

    def test_stoch_in_range(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        fv = fb.build(m15_bars.index[-1])
        stoch_i = FEATURE_ORDER.index("stoch_k_14")
        # Normalized to [0, 1] by builder
        assert 0 <= fv.values[stoch_i] <= 1

    def test_bb_position_clipped(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        fv = fb.build(m15_bars.index[-1])
        bb_i = FEATURE_ORDER.index("bb_position_20")
        assert 0 <= fv.values[bb_i] <= 1

    def test_session_flags_binary(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        fv = fb.build(m15_bars.index[-1])
        for name in ("is_london_session", "is_ny_session", "is_london_ny_overlap"):
            v = fv.values[FEATURE_ORDER.index(name)]
            assert v in (0.0, 1.0), f"{name}={v} not binary"

    def test_hour_sin_cos_in_range(self, m15_bars, h1_bars):
        fb = FeatureBuilder(m15_bars, h1_bars, "EURUSD")
        fv = fb.build(m15_bars.index[-1])
        for name in ("hour_utc_sin", "hour_utc_cos", "day_of_week_sin"):
            v = fv.values[FEATURE_ORDER.index(name)]
            assert -1.0 <= v <= 1.0
