"""Validation and immutable manifests for historical replay inputs."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtesting.data_loader import load_ohlc_csv


@dataclass(frozen=True)
class DatasetManifest:
    schema_version: int
    symbol: str
    source_file: str
    sha256: str
    rows: int
    start_utc: str
    end_utc: str
    median_bar_seconds: float
    columns: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_ohlc_frame(frame: pd.DataFrame) -> None:
    if frame.empty:
        raise ValueError("OHLC dataset contains no rows")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("OHLC dataset index must be timestamps")
    if frame.index.has_duplicates:
        raise ValueError("OHLC dataset contains duplicate timestamps")
    if not frame.index.is_monotonic_increasing:
        raise ValueError("OHLC dataset timestamps are not chronological")
    if frame.index.tz is None:
        raise ValueError("OHLC dataset timestamps must be timezone-aware")

    required = ["open", "high", "low", "close"]
    values = frame[required].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(values.to_numpy(dtype=float)).all():
        raise ValueError("OHLC dataset contains non-finite prices")
    if (values <= 0).any().any():
        raise ValueError("OHLC dataset contains non-positive prices")
    if (values["high"] < values[["open", "close"]].max(axis=1)).any():
        raise ValueError("OHLC dataset has high below open or close")
    if (values["low"] > values[["open", "close"]].min(axis=1)).any():
        raise ValueError("OHLC dataset has low above open or close")
    if (values["high"] < values["low"]).any():
        raise ValueError("OHLC dataset has high below low")


def inspect_ohlc_csv(path: str | Path, *, symbol: str) -> DatasetManifest:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"OHLC CSV not found: {source}")
    clean_symbol = symbol.strip().upper()
    if not clean_symbol.isalpha() or len(clean_symbol) != 6:
        raise ValueError("symbol must be a six-letter FX pair, for example EURUSD")
    frame = load_ohlc_csv(source)
    validate_ohlc_frame(frame)
    deltas = frame.index.to_series().diff().dropna().dt.total_seconds()
    return DatasetManifest(
        schema_version=1,
        symbol=clean_symbol,
        source_file=source.name,
        sha256=_file_hash(source),
        rows=len(frame),
        start_utc=frame.index[0].isoformat(),
        end_utc=frame.index[-1].isoformat(),
        median_bar_seconds=float(deltas.median()) if not deltas.empty else 0.0,
        columns=list(frame.columns),
    )
