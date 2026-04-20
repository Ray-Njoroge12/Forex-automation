"""Tests for extract_data.py — MT5 historical data extraction.

These tests use mocks so they can run on any OS (MT5 is Windows-only).
Running against a real MT5 connection is validated separately on Windows.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from ml.meta_labeler import extract_data


# ══════════════════════════════════════════════════════════════════════
# bars_to_dataframe — pure transformation, easy to test
# ══════════════════════════════════════════════════════════════════════

class TestBarsToDataFrame:
    def _make_rates(self, n: int = 5) -> np.ndarray:
        """Build a synthetic MT5 rates array (numpy structured array)."""
        dtype = np.dtype([
            ("time", "i8"),
            ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
            ("tick_volume", "i8"),
            ("spread", "i4"),
            ("real_volume", "i8"),
        ])
        base = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        rates = np.zeros(n, dtype=dtype)
        for i in range(n):
            rates["time"][i] = base + i * 900  # M15 = 900s apart
            rates["open"][i] = 1.1000 + i * 0.0001
            rates["high"][i] = 1.1005 + i * 0.0001
            rates["low"][i] = 1.0995 + i * 0.0001
            rates["close"][i] = 1.1002 + i * 0.0001
            rates["tick_volume"][i] = 1000 + i
            rates["spread"][i] = 10 + i
            rates["real_volume"][i] = 0
        return rates

    def test_empty_input_returns_empty(self):
        empty = np.array([], dtype=[("time", "i8"), ("close", "f8")])
        out = extract_data.bars_to_dataframe(empty)
        assert out.empty

    def test_converts_time_to_utc_index(self):
        rates = self._make_rates(3)
        df = extract_data.bars_to_dataframe(rates)
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is not None
        assert str(df.index.tz) == "UTC"

    def test_renames_tick_volume_to_volume(self):
        rates = self._make_rates(3)
        df = extract_data.bars_to_dataframe(rates)
        assert "volume" in df.columns
        assert "tick_volume" not in df.columns

    def test_drops_real_volume(self):
        rates = self._make_rates(3)
        df = extract_data.bars_to_dataframe(rates)
        assert "real_volume" not in df.columns

    def test_enforces_types(self):
        rates = self._make_rates(3)
        df = extract_data.bars_to_dataframe(rates)
        assert df["close"].dtype == np.float64
        assert df["volume"].dtype == np.int64
        assert df["spread"].dtype == np.int32

    def test_sorted_ascending(self):
        rates = self._make_rates(5)
        # shuffle order by reversing
        rates = rates[::-1]
        df = extract_data.bars_to_dataframe(rates)
        assert df.index.is_monotonic_increasing


# ══════════════════════════════════════════════════════════════════════
# save / load Parquet — round-trip correctness
# ══════════════════════════════════════════════════════════════════════

class TestParquetRoundTrip:
    def test_save_and_load(self, tmp_path):
        df = pd.DataFrame(
            {
                "open": [1.1, 1.2, 1.3],
                "high": [1.15, 1.25, 1.35],
                "low": [1.05, 1.15, 1.25],
                "close": [1.12, 1.22, 1.32],
                "volume": [100, 200, 300],
                "spread": np.array([10, 11, 12], dtype="int32"),
            },
            index=pd.date_range("2024-01-01", periods=3, freq="15min", tz="UTC"),
        )
        path = extract_data.save_parquet(df, "EURUSD", "M15", tmp_path)
        assert path.exists()
        assert path.name == "EURUSD_M15.parquet"

        loaded = extract_data.load_parquet("EURUSD", "M15", tmp_path)
        # Parquet does not preserve index.freq — verify data equivalence
        # by comparing values explicitly.
        assert list(loaded.columns) == list(df.columns)
        assert loaded.shape == df.shape
        np.testing.assert_array_equal(loaded["open"].values, df["open"].values)
        np.testing.assert_array_equal(loaded["close"].values, df["close"].values)
        np.testing.assert_array_equal(loaded["volume"].values, df["volume"].values)
        # Index timestamps preserved, just freq attribute is lost
        assert (loaded.index == df.index).all()

    def test_load_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No extracted data"):
            extract_data.load_parquet("EURUSD", "M15", tmp_path)


# ══════════════════════════════════════════════════════════════════════
# Lazy import guard
# ══════════════════════════════════════════════════════════════════════

class TestMT5Lazy:
    def test_require_mt5_raises_helpful_error_on_linux(self):
        """On Linux, MT5 is not importable — verify error message."""
        with patch.dict("sys.modules", {"MetaTrader5": None}):
            with pytest.raises(RuntimeError, match="MetaTrader5 package not available"):
                extract_data._require_mt5()

    def test_shutdown_is_safe_without_mt5(self):
        """shutdown_mt5() on a system without MT5 should not raise."""
        with patch.dict("sys.modules", {"MetaTrader5": None}):
            # Should be a no-op, not raise
            extract_data.shutdown_mt5()


# ══════════════════════════════════════════════════════════════════════
# extract_symbol with mocked MT5
# ══════════════════════════════════════════════════════════════════════

class TestExtractSymbolMocked:
    def _mock_mt5(self, rates=None):
        """Build a mock MT5 module. Returns (mock_mt5, patch_context)."""
        mock_mt5 = MagicMock()
        mock_mt5.TIMEFRAME_M1 = 1
        mock_mt5.TIMEFRAME_M5 = 5
        mock_mt5.TIMEFRAME_M15 = 15
        mock_mt5.TIMEFRAME_M30 = 30
        mock_mt5.TIMEFRAME_H1 = 16385
        mock_mt5.TIMEFRAME_H4 = 16388
        mock_mt5.TIMEFRAME_D1 = 16408
        mock_mt5.symbol_select.return_value = True
        mock_mt5.copy_rates_range.return_value = rates
        mock_mt5.last_error.return_value = (0, "OK")
        mock_mt5.initialize.return_value = True
        return mock_mt5

    def test_extract_symbol_returns_dataframe(self):
        # Build valid rates
        dtype = np.dtype([
            ("time", "i8"), ("open", "f8"), ("high", "f8"),
            ("low", "f8"), ("close", "f8"),
            ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8"),
        ])
        rates = np.zeros(3, dtype=dtype)
        rates["time"] = [1704067200, 1704068100, 1704069000]  # 3 bars
        rates["open"] = [1.10, 1.11, 1.12]
        rates["high"] = [1.11, 1.12, 1.13]
        rates["low"] = [1.09, 1.10, 1.11]
        rates["close"] = [1.105, 1.115, 1.125]
        rates["tick_volume"] = [1000, 1100, 1200]
        rates["spread"] = [10, 11, 12]

        mock_mt5 = self._mock_mt5(rates)
        with patch.object(extract_data, "_require_mt5", return_value=mock_mt5):
            df = extract_data.extract_symbol("EURUSD", "M15", years_back=1)

        assert len(df) == 3
        assert "close" in df.columns
        mock_mt5.symbol_select.assert_called_once_with("EURUSD", True)
        mock_mt5.copy_rates_range.assert_called_once()
        # Verify the timeframe constant was used
        call_args = mock_mt5.copy_rates_range.call_args
        assert call_args[0][1] == 15  # TIMEFRAME_M15

    def test_unknown_timeframe_raises(self):
        mock_mt5 = self._mock_mt5()
        with patch.object(extract_data, "_require_mt5", return_value=mock_mt5):
            with pytest.raises(ValueError, match="Unknown timeframe"):
                extract_data.extract_symbol("EURUSD", "M99")

    def test_symbol_select_failure_raises(self):
        mock_mt5 = self._mock_mt5()
        mock_mt5.symbol_select.return_value = False
        with patch.object(extract_data, "_require_mt5", return_value=mock_mt5):
            with pytest.raises(RuntimeError, match="symbol_select"):
                extract_data.extract_symbol("FAKEPAIR", "M15")

    def test_empty_response_raises(self):
        mock_mt5 = self._mock_mt5(rates=None)
        with patch.object(extract_data, "_require_mt5", return_value=mock_mt5):
            with pytest.raises(RuntimeError, match="No data returned"):
                extract_data.extract_symbol("EURUSD", "M15")


# ══════════════════════════════════════════════════════════════════════
# Manifest round-trip
# ══════════════════════════════════════════════════════════════════════

class TestManifest:
    def test_write_and_load(self, tmp_path):
        results = {
            "EURUSD_M15": extract_data.ExtractionResult(
                symbol="EURUSD",
                timeframe="M15",
                rows=1000,
                start=pd.Timestamp("2020-01-01", tz="UTC"),
                end=pd.Timestamp("2025-01-01", tz="UTC"),
                path=tmp_path / "EURUSD_M15.parquet",
                extracted_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
        }
        extract_data._write_manifest(results, tmp_path)
        loaded = extract_data.load_manifest(tmp_path)
        assert "EURUSD_M15" in loaded
        assert loaded["EURUSD_M15"]["rows"] == 1000
        assert loaded["EURUSD_M15"]["symbol"] == "EURUSD"

    def test_missing_manifest_returns_empty(self, tmp_path):
        assert extract_data.load_manifest(tmp_path) == {}
