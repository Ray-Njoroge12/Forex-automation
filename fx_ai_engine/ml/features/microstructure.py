"""Microstructure features — spread and volume.

MT5's 'spread' field is in POINTS, not pips:
    5-digit brokers (most majors):  1 pip = 10 points
    3-digit JPY brokers:            1 pip = 10 points
So we divide the raw spread value by 10 to get pips. If future brokers
use different conventions, override PIP_SIZE_POINTS per symbol.
"""
from __future__ import annotations

from typing import Final

import numpy as np
import pandas as pd

# MT5 'spread' field units → number of points per pip for each symbol.
# Default convention: 10 points per pip (5-digit / 3-digit brokers).
PIP_SIZE_POINTS: Final[dict[str, int]] = {
    "EURUSD": 10,
    "GBPUSD": 10,
    "USDJPY": 10,
    "AUDUSD": 10,
    "USDCAD": 10,
    "USDCHF": 10,
    "NZDUSD": 10,
    "EURJPY": 10,
    "EURGBP": 10,
}

# Pip size in price units, used to convert price distance → pips.
# 4-digit majors: 1 pip = 0.0001
# JPY pairs: 1 pip = 0.01
PIP_IN_PRICE: Final[dict[str, float]] = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "AUDUSD": 0.0001,
    "USDCAD": 0.0001,
    "USDCHF": 0.0001,
    "NZDUSD": 0.0001,
    "EURJPY": 0.01,
    "EURGBP": 0.0001,
}


def spread_to_pips(spread_points: float, symbol: str) -> float:
    """Convert MT5 'spread' field (in points) to pips.

    Unknown symbols default to 10 points/pip (safest for majors).
    """
    divisor = PIP_SIZE_POINTS.get(symbol.upper(), 10)
    if divisor <= 0:
        return 0.0
    return float(spread_points) / divisor


def price_distance_to_pips(price_distance: float, symbol: str) -> float:
    """Convert a price distance (e.g. ATR in price units) to pips."""
    pip_size = PIP_IN_PRICE.get(symbol.upper(), 0.0001)
    if pip_size <= 0:
        return 0.0
    return float(price_distance) / pip_size


def volume_zscore(volume_series: pd.Series, window: int = 20) -> float:
    """Z-score of current volume vs the PRIOR `window` bars (not including
    the current bar). This measures how unusual the current bar's volume
    is compared to its recent history.

    Returns 0.0 if insufficient history or prior std is 0/NaN.
    """
    if len(volume_series) < window + 1:
        return 0.0
    # Prior window, NOT including the current bar
    prior = volume_series.iloc[-window - 1:-1].astype("float64")
    mean = prior.mean()
    std = prior.std(ddof=0)
    if std == 0 or np.isnan(std):
        return 0.0
    current = float(volume_series.iloc[-1])
    result = (current - mean) / std
    return float(result) if np.isfinite(result) else 0.0


def volume_vs_ma(volume_series: pd.Series, window: int = 20) -> float:
    """Current volume divided by the MA of the PRIOR `window` bars.
    A value of 2.0 means current volume is double the recent average.

    Returns 1.0 (neutral) if insufficient history or prior MA is 0/NaN.
    """
    if len(volume_series) < window + 1:
        return 1.0
    prior = volume_series.iloc[-window - 1:-1].astype("float64")
    mean = prior.mean()
    if mean == 0 or np.isnan(mean):
        return 1.0
    result = float(volume_series.iloc[-1]) / mean
    return float(result) if np.isfinite(result) else 1.0
