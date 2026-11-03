"""Server-time to UTC conversion for MT5 history exports."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from backtesting.data_loader import load_ohlc_csv
from validation.convert_mt5_history import convert, ny_plus_7_offset_seconds


def _server_epoch(text: str) -> int:
    """Naive server wall-clock time as MT5 stores it (epoch seconds)."""
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


@pytest.mark.parametrize(
    "server_time, expected_hours",
    [
        ("2026-01-15 12:00:00", 2),  # US standard time -> GMT+2
        ("2026-07-15 12:00:00", 3),  # US daylight time -> GMT+3
        ("2026-11-03 12:00:00", 2),  # after US DST ended on 1 Nov 2026
    ],
)
def test_ny_plus_7_offsets(server_time, expected_hours):
    assert ny_plus_7_offset_seconds(_server_epoch(server_time)) == expected_hours * 3600


def _write_export(tmp_path, rows, offset):
    src = tmp_path / "EURUSD_M15.csv"
    pd.DataFrame(rows, columns=["server_time", "open", "high", "low", "close", "tick_volume", "spread_points"]).to_csv(src, index=False)
    src.with_suffix(".meta.json").write_text(json.dumps({"export_server_utc_offset_seconds": offset}))
    return src


def test_convert_applies_seasonal_offset_and_loads(tmp_path):
    rows = [
        [_server_epoch("2026-07-15 12:00:00"), 1.1, 1.2, 1.0, 1.15, 10, 5],
        [_server_epoch("2026-11-03 12:00:00"), 1.1, 1.2, 1.0, 1.15, 10, 5],
    ]
    src = _write_export(tmp_path, rows, 7200)
    out = tmp_path / "out.csv"
    report = convert(src, out)
    assert report["rows"] == 2
    frame = load_ohlc_csv(out)
    assert frame.index[0] == pd.Timestamp("2026-07-15 09:00:00", tz="UTC")
    assert frame.index[1] == pd.Timestamp("2026-11-03 10:00:00", tz="UTC")


def test_convert_refuses_contradicting_export_offset(tmp_path):
    rows = [[_server_epoch("2026-11-03 12:00:00"), 1.1, 1.2, 1.0, 1.15, 10, 5]]
    src = _write_export(tmp_path, rows, 10800)
    with pytest.raises(ValueError, match="contradicts ny_plus_7"):
        convert(src, tmp_path / "out.csv")
