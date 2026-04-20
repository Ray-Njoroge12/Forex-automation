"""Time and session-based features.

All times are UTC. Session boundaries match the forex market convention:
    London: 07:00 - 16:00 UTC
    New York: 12:00 - 21:00 UTC
    Overlap: 12:00 - 16:00 UTC (highest liquidity window)

Cyclic encoding (sin/cos) is used for hour and day-of-week so the model
sees 23:00 and 01:00 as close, not far apart.
"""
from __future__ import annotations

import math

from ml.features.schema import REGIME_ENCODING


def hour_sin_cos(hour_utc: int) -> tuple[float, float]:
    """Cyclic encoding of UTC hour [0-23].

    Returns (sin, cos) pair. Model can recover hour from both components
    but treats adjacent hours (23 → 0) as close, not as a 23-unit jump.
    """
    if not 0 <= hour_utc <= 23:
        raise ValueError(f"hour_utc must be 0-23, got {hour_utc}")
    angle = 2 * math.pi * hour_utc / 24
    return math.sin(angle), math.cos(angle)


def dow_sin(day_of_week: int) -> float:
    """Cyclic sin encoding of day of week. Monday=0 … Sunday=6."""
    if not 0 <= day_of_week <= 6:
        raise ValueError(f"day_of_week must be 0-6, got {day_of_week}")
    return math.sin(2 * math.pi * day_of_week / 7)


def is_london_session(hour_utc: int) -> int:
    """London session: 07:00 ≤ hour < 16:00 UTC."""
    return 1 if 7 <= hour_utc < 16 else 0


def is_ny_session(hour_utc: int) -> int:
    """New York session: 12:00 ≤ hour < 21:00 UTC."""
    return 1 if 12 <= hour_utc < 21 else 0


def is_london_ny_overlap(hour_utc: int) -> int:
    """London-NY overlap: 12:00 ≤ hour < 16:00 UTC (highest liquidity)."""
    return 1 if 12 <= hour_utc < 16 else 0


def encode_regime(regime_label: str | None) -> int:
    """Ordinal encoding of regime label per FEATURE_SCHEMA_VERSION.

    Unknown labels default to 0 (same as RANGE) — the model sees "no clear
    trend" as the safe default. This is intentionally forgiving so a new
    regime label added to RegimeAgent doesn't crash inference.
    """
    if regime_label is None:
        return REGIME_ENCODING["UNKNOWN"]
    key = str(regime_label).upper().strip()
    return REGIME_ENCODING.get(key, REGIME_ENCODING["UNKNOWN"])
