"""FeatureBuilder — assembles the 30-feature vector for meta-labeling.

Used identically in training pipeline and live AdversarialAgent.

Performance design:
    1. __init__ calls indicators.annotate() ONCE on both DataFrames.
    2. build() is a fast row-lookup + a few rolling window computations
       on small slices (<100 bars), so per-candidate cost is microseconds.
    3. No mutation of input DataFrames.

Safety design:
    1. FEATURE_ORDER is enforced on every build() call (missing or extra
       feature = RuntimeError).
    2. Non-finite values (NaN, Inf) in output = RuntimeError. Upstream
       must handle or skip the candidate.
    3. No look-ahead: .loc[:candidate_time] slices to point-in-time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from ml.features import indicators, microstructure, session
from ml.features.schema import FEATURE_ORDER, FEATURE_SCHEMA_VERSION

# Minimum lookback needed to produce valid features.
# - EMA(200) on H1: 200 bars before candidate
# - realized_vol_20 + log_ret_20 + rolling windows: ~30 bars on M15
_MIN_M15_BARS: int = 30
_MIN_H1_BARS: int = 200


@dataclass(frozen=True)
class FeatureVector:
    """An ordered, versioned feature vector ready for ML inference.

    Attributes:
        values: np.ndarray of shape (30,), float32, in FEATURE_ORDER order.
        names: tuple of feature names, same length as values.
        timestamp: bar close timestamp this vector describes.
        schema_version: version string matching FEATURE_SCHEMA_VERSION.
    """

    values: np.ndarray
    names: tuple[str, ...]
    timestamp: pd.Timestamp
    schema_version: str

    def __post_init__(self) -> None:
        if self.values.shape != (len(FEATURE_ORDER),):
            raise ValueError(
                f"Expected shape ({len(FEATURE_ORDER)},), got {self.values.shape}"
            )
        if self.values.dtype != np.float32:
            raise ValueError(f"Expected float32 dtype, got {self.values.dtype}")
        if len(self.names) != len(self.values):
            raise ValueError("names and values must have the same length")

    def as_dict(self) -> dict[str, float]:
        """Return a {name: value} dict for logging / debugging."""
        return {n: float(v) for n, v in zip(self.names, self.values)}


class FeatureBuilder:
    """Builds the 30-feature vector for a candidate signal."""

    def __init__(
        self,
        m15_df: pd.DataFrame,
        h1_df: pd.DataFrame,
        symbol: str,
    ) -> None:
        """Precompute all indicators on both timeframes.

        Args:
            m15_df: M15 OHLCV DataFrame with UTC DatetimeIndex.
                    Must have columns: open, high, low, close, volume.
                    'spread' is optional.
            h1_df: H1 OHLCV DataFrame, same schema as m15_df.
            symbol: e.g. "EURUSD". Used for pip-size conversion.
        """
        if not isinstance(m15_df.index, pd.DatetimeIndex):
            raise TypeError("m15_df must have a DatetimeIndex")
        if not isinstance(h1_df.index, pd.DatetimeIndex):
            raise TypeError("h1_df must have a DatetimeIndex")
        if m15_df.empty or h1_df.empty:
            raise ValueError("Input DataFrames must not be empty")

        self.symbol = symbol.upper()
        # Annotating each once is the performance optimization.
        self.m15 = indicators.annotate(m15_df)
        self.h1 = indicators.annotate(h1_df)

    def build(
        self,
        candidate_time: pd.Timestamp,
        regime_label: Optional[str] = None,
    ) -> FeatureVector:
        """Build the 30-feature vector for a single candidate.

        Args:
            candidate_time: timestamp at which to evaluate features.
                All features use data available at or before this time.
            regime_label: regime label from RegimeAgent (e.g. "TREND_UP").
                May be None → encoded as UNKNOWN.

        Raises:
            ValueError: insufficient history.
            RuntimeError: missing/extra features, non-finite values.
        """
        # Point-in-time slices — no look-ahead.
        m15_hist = self.m15.loc[:candidate_time]
        h1_hist = self.h1.loc[:candidate_time]

        if len(m15_hist) < _MIN_M15_BARS:
            raise ValueError(
                f"Insufficient M15 history at {candidate_time}: "
                f"have {len(m15_hist)}, need {_MIN_M15_BARS}"
            )
        if len(h1_hist) < _MIN_H1_BARS:
            raise ValueError(
                f"Insufficient H1 history at {candidate_time}: "
                f"have {len(h1_hist)}, need {_MIN_H1_BARS}"
            )

        m15_bar = m15_hist.iloc[-1]
        h1_bar = h1_hist.iloc[-1]

        feats = self._compute_features(m15_hist, h1_hist, m15_bar, h1_bar, regime_label)

        # Enforce contract: must be exactly FEATURE_ORDER
        missing = set(FEATURE_ORDER) - set(feats)
        extra = set(feats) - set(FEATURE_ORDER)
        if missing:
            raise RuntimeError(f"Missing features: {missing}")
        if extra:
            raise RuntimeError(f"Unexpected features: {extra}")

        values = np.array(
            [feats[name] for name in FEATURE_ORDER], dtype=np.float32
        )
        if not np.all(np.isfinite(values)):
            bad = [
                n for n, v in zip(FEATURE_ORDER, values) if not np.isfinite(v)
            ]
            raise RuntimeError(f"Non-finite features: {bad}")

        return FeatureVector(
            values=values,
            names=FEATURE_ORDER,
            timestamp=candidate_time,
            schema_version=FEATURE_SCHEMA_VERSION,
        )

    # ------------------------------------------------------------------
    # Feature computation (private)
    # ------------------------------------------------------------------

    def _compute_features(
        self,
        m15_hist: pd.DataFrame,
        h1_hist: pd.DataFrame,
        m15_bar: pd.Series,
        h1_bar: pd.Series,
        regime_label: Optional[str],
    ) -> dict[str, float]:
        """Compute all 30 features. Returns a plain dict {name: float}."""
        close = float(m15_bar["close"])
        atr14 = float(m15_bar["atr_14"])

        # ───── Group A · Price Dynamics (8) ─────
        feats: dict[str, float] = {}
        feats["log_ret_1"] = _safe_log_return(m15_hist["close"], 1)
        feats["log_ret_5"] = _safe_log_return(m15_hist["close"], 5)
        feats["log_ret_20"] = _safe_log_return(m15_hist["close"], 20)
        feats["realized_vol_20"] = _realized_vol(m15_hist["close"], 20)
        feats["atr_14_ratio"] = _safe_div(atr14, close)
        feats["atr_5_vs_20"] = _safe_div(
            float(m15_bar["atr_5"]), float(m15_bar["atr_20"])
        )
        feats["close_vs_ema50"] = _safe_div(
            close - float(m15_bar["ema_50"]), atr14
        )
        feats["ema50_vs_ema200"] = _safe_div(
            float(m15_bar["ema_50"]) - float(m15_bar["ema_200"]),
            float(m15_bar["ema_200"]),
        )

        # ───── Group B · Technical Indicators (8) ─────
        feats["rsi_14"] = float(m15_bar["rsi_14"])
        feats["rsi_14_slope_5"] = _slope_last_n(m15_hist["rsi_14"], 5)
        feats["adx_14"] = float(m15_bar["adx_14"])
        feats["adx_slope_5"] = _diff_last_n(m15_hist["adx_14"], 5)
        bb_upper = float(m15_bar["bb_upper_20"])
        bb_lower = float(m15_bar["bb_lower_20"])
        bb_middle = float(m15_bar["bb_middle_20"])
        bb_range = bb_upper - bb_lower
        feats["bb_width_20"] = _safe_div(bb_range, bb_middle)
        feats["bb_position_20"] = _clip01(_safe_div(close - bb_lower, bb_range))
        feats["macd_hist"] = _safe_div(float(m15_bar["macd_hist"]), close)
        feats["stoch_k_14"] = float(m15_bar["stoch_k_14"]) / 100.0

        # ───── Group C · Microstructure (4) ─────
        spread_val = float(m15_bar.get("spread", 0) or 0)
        spread_pips = microstructure.spread_to_pips(spread_val, self.symbol)
        atr_pips = microstructure.price_distance_to_pips(atr14, self.symbol)
        feats["spread_pips"] = spread_pips
        feats["spread_vs_atr"] = _safe_div(spread_pips, atr_pips)

        if "volume" in m15_hist.columns:
            vol_series = m15_hist["volume"]
            feats["volume_z_20"] = microstructure.volume_zscore(vol_series, 20)
            feats["volume_vs_ma20"] = microstructure.volume_vs_ma(vol_series, 20)
        else:
            feats["volume_z_20"] = 0.0
            feats["volume_vs_ma20"] = 1.0

        # ───── Group D · Time & Session (6) ─────
        ts = m15_hist.index[-1]
        hour = int(ts.hour)
        dow = int(ts.dayofweek)
        h_sin, h_cos = session.hour_sin_cos(hour)
        feats["hour_utc_sin"] = h_sin
        feats["hour_utc_cos"] = h_cos
        feats["day_of_week_sin"] = session.dow_sin(dow)
        feats["is_london_session"] = float(session.is_london_session(hour))
        feats["is_ny_session"] = float(session.is_ny_session(hour))
        feats["is_london_ny_overlap"] = float(session.is_london_ny_overlap(hour))

        # ───── Group E · Regime Context from H1 (4) ─────
        feats["h1_ema50_vs_ema200"] = _safe_div(
            float(h1_bar["ema_50"]) - float(h1_bar["ema_200"]),
            float(h1_bar["ema_200"]),
        )
        feats["h1_adx_14"] = float(h1_bar["adx_14"])
        feats["h1_atr_vs_m15_atr"] = _safe_div(float(h1_bar["atr_14"]), atr14)
        feats["regime_encoded"] = float(session.encode_regime(regime_label))

        return feats


# ══════════════════════════════════════════════════════════════════════
# Pure helper functions
# ══════════════════════════════════════════════════════════════════════

def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Division that returns `default` on zero or non-finite denominator/result."""
    if denominator == 0 or not np.isfinite(denominator):
        return default
    result = numerator / denominator
    return float(result) if np.isfinite(result) else default


def _safe_log_return(series: pd.Series, lag: int) -> float:
    """Log return over `lag` bars. Returns 0.0 on invalid inputs."""
    if len(series) < lag + 1:
        return 0.0
    current = float(series.iloc[-1])
    past = float(series.iloc[-1 - lag])
    if past <= 0 or current <= 0:
        return 0.0
    result = np.log(current / past)
    return float(result) if np.isfinite(result) else 0.0


def _realized_vol(series: pd.Series, window: int) -> float:
    """Realized volatility: std of 1-bar log returns over `window` bars,
    scaled by sqrt(window) to give a cumulative-horizon number."""
    if len(series) < window + 1:
        return 0.0
    tail = series.iloc[-window - 1:]
    log_rets = np.log(tail / tail.shift(1)).dropna()
    if len(log_rets) == 0:
        return 0.0
    std_val = float(log_rets.std(ddof=0))
    if not np.isfinite(std_val):
        return 0.0
    return std_val * np.sqrt(window)


def _slope_last_n(series: pd.Series, n: int) -> float:
    """Linear regression slope of last n values, ignoring leading NaNs."""
    recent = series.iloc[-n:].dropna()
    if len(recent) < 2:
        return 0.0
    x = np.arange(len(recent), dtype=np.float64)
    y = recent.values.astype(np.float64)
    # polyfit on constant y gives slope = 0
    try:
        slope = np.polyfit(x, y, 1)[0]
    except (np.linalg.LinAlgError, ValueError):
        return 0.0
    return float(slope) if np.isfinite(slope) else 0.0


def _diff_last_n(series: pd.Series, n: int) -> float:
    """Difference between current and value n bars ago."""
    if len(series) < n + 1:
        return 0.0
    current = float(series.iloc[-1])
    past = float(series.iloc[-1 - n])
    if not (np.isfinite(current) and np.isfinite(past)):
        return 0.0
    return current - past


def _clip01(value: float) -> float:
    """Clip to [0, 1] range (for bb_position which can exceed bands)."""
    if not np.isfinite(value):
        return 0.5
    return float(max(0.0, min(1.0, value)))
