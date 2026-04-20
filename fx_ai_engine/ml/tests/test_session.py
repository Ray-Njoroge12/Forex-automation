"""Tests for session.py — time/regime encoding."""
from __future__ import annotations

import math

import pytest

from ml.features import session


class TestHourSinCos:
    def test_00_hour(self):
        s, c = session.hour_sin_cos(0)
        assert s == pytest.approx(0.0)
        assert c == pytest.approx(1.0)

    def test_12_hour(self):
        s, c = session.hour_sin_cos(12)
        assert s == pytest.approx(0.0, abs=1e-10)
        assert c == pytest.approx(-1.0)

    def test_6_hour(self):
        """6:00 → angle = π/2 → sin=1, cos=0."""
        s, c = session.hour_sin_cos(6)
        assert s == pytest.approx(1.0)
        assert c == pytest.approx(0.0, abs=1e-10)

    def test_cyclic_wraparound(self):
        """23:00 and 01:00 should be similar (not far apart)."""
        s23, c23 = session.hour_sin_cos(23)
        s1, c1 = session.hour_sin_cos(1)
        # Distance in sin/cos space should be small
        dist = math.hypot(s23 - s1, c23 - c1)
        assert dist < 0.55, f"23:00 and 01:00 are {dist} apart"

    def test_invalid_hour(self):
        with pytest.raises(ValueError):
            session.hour_sin_cos(-1)
        with pytest.raises(ValueError):
            session.hour_sin_cos(24)


class TestDowSin:
    def test_valid_range(self):
        for dow in range(7):
            # Should not raise and should be in [-1, 1]
            v = session.dow_sin(dow)
            assert -1 <= v <= 1

    def test_invalid_dow(self):
        with pytest.raises(ValueError):
            session.dow_sin(-1)
        with pytest.raises(ValueError):
            session.dow_sin(7)


class TestLondonSession:
    def test_in_session(self):
        for h in range(7, 16):
            assert session.is_london_session(h) == 1

    def test_out_of_session(self):
        for h in (0, 6, 16, 17, 22, 23):
            assert session.is_london_session(h) == 0


class TestNYSession:
    def test_in_session(self):
        for h in range(12, 21):
            assert session.is_ny_session(h) == 1

    def test_out_of_session(self):
        for h in (0, 11, 21, 22, 23):
            assert session.is_ny_session(h) == 0


class TestLondonNYOverlap:
    def test_overlap_window(self):
        for h in range(12, 16):
            assert session.is_london_ny_overlap(h) == 1

    def test_outside_overlap(self):
        for h in (0, 7, 11, 16, 17, 20):
            assert session.is_london_ny_overlap(h) == 0

    def test_overlap_implies_both_sessions(self):
        for h in range(12, 16):
            assert session.is_london_session(h) == 1
            assert session.is_ny_session(h) == 1


class TestEncodeRegime:
    def test_known_labels(self):
        assert session.encode_regime("RANGE") == 0
        assert session.encode_regime("TRANSITION") == 1
        assert session.encode_regime("TREND_UP") == 2
        assert session.encode_regime("TREND_DOWN") == 2

    def test_case_insensitive(self):
        assert session.encode_regime("trend_up") == 2
        assert session.encode_regime("Range") == 0

    def test_whitespace_tolerant(self):
        assert session.encode_regime("  TREND  ") == 2

    def test_unknown_defaults_to_0(self):
        assert session.encode_regime("UNRECOGNIZED_LABEL") == 0

    def test_none_defaults_to_0(self):
        assert session.encode_regime(None) == 0
