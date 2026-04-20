"""Technical indicators — pure pandas/numpy implementations.

Design principles:
    1. No dependency on external TA libraries (pandas-ta, ta) in the core
       path. We want bit-exact reproducibility across training and live.
    2. Wilder smoothing is implemented as EWM with alpha=1/period. This
       matches TradingView / MT5 / Lopez de Prado conventions exactly.
    3. EMAs use pandas ewm(span=n, adjust=False) — matches MT5.
    4. All functions gracefully handle NaN during warmup (min_periods).
    5. `annotate()` precomputes every indicator the FeatureBuilder will
       need so per-candidate feature extraction is a fast row lookup.

Functions return pd.Series (aligned to input index). Use `annotate()`
to get a DataFrame with all indicators attached.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════════════════
# Moving Averages
# ══════════════════════════════════════════════════════════════════════

def ema(series: pd.Series, period: int) -> pd.Series:
    """Standard EMA using pandas ewm with adjust=False (matches MT5).

    The adjust=False convention means:
        y[0] = x[0]
        y[t] = alpha * x[t] + (1 - alpha) * y[t-1]
    where alpha = 2 / (period + 1).
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    return series.rolling(window=period, min_periods=period).mean()


# ══════════════════════════════════════════════════════════════════════
# True Range / Average True Range (Wilder smoothing)
# ══════════════════════════════════════════════════════════════════════

def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range = max(H-L, |H-C_prev|, |L-C_prev|).

    First bar has no prev close, so TR = H-L for that bar.
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    hl = high - low
    hc = (high - prev_close).abs()
    lc = (low - prev_close).abs()

    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    # First bar: only H-L is defined
    tr.iloc[0] = hl.iloc[0]
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR using Wilder smoothing.

    Wilder smoothing is equivalent to EWM with alpha = 1/period
    (NOT the standard EWM where alpha = 2/(period+1)).
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    tr = true_range(df)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# ══════════════════════════════════════════════════════════════════════
# Relative Strength Index (Wilder)
# ══════════════════════════════════════════════════════════════════════

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI using Wilder smoothing.

    RSI = 100 - (100 / (1 + RS))
        RS = avg_gain / avg_loss
    When avg_loss == 0, RSI = 100 by definition.
    When avg_gain == 0 and avg_loss > 0, RSI = 0.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    # Compute RSI while handling edge cases
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    # Where avg_loss == 0 and avg_gain > 0: pure up-move, RSI = 100
    rsi_val = rsi_val.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    # Where both are 0 (flat): convention = 50
    rsi_val = rsi_val.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return rsi_val


# ══════════════════════════════════════════════════════════════════════
# Average Directional Index (Wilder)
# ══════════════════════════════════════════════════════════════════════

def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ADX.

    1. Compute +DM and -DM from high/low movements.
    2. Smooth TR, +DM, -DM with Wilder smoothing (alpha = 1/period).
    3. +DI = 100 * smoothed(+DM) / smoothed(TR)
       -DI = 100 * smoothed(-DM) / smoothed(TR)
    4. DX = 100 * |+DI - -DI| / (+DI + -DI)
    5. ADX = Wilder-smoothed DX.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    high = df["high"]
    low = df["low"]

    up_move = high.diff()
    down_move = -low.diff()  # positive when price moves DOWN

    # +DM fires only when up_move > down_move AND up_move > 0
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    )
    # -DM fires only when down_move > up_move AND down_move > 0
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=low.index,
    )

    tr = true_range(df)

    # Wilder smoothing on all three
    alpha = 1 / period
    smoothed_tr = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    # Guard against divide-by-zero
    plus_di = 100 * smoothed_plus_dm / smoothed_tr.replace(0, np.nan)
    minus_di = 100 * smoothed_minus_dm / smoothed_tr.replace(0, np.nan)

    di_sum = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan)

    adx_val = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return adx_val


# ══════════════════════════════════════════════════════════════════════
# MACD
# ══════════════════════════════════════════════════════════════════════

def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Standard MACD. Returns (macd_line, signal_line, histogram)."""
    if not (0 < fast < slow):
        raise ValueError(f"Require 0 < fast < slow, got fast={fast} slow={slow}")
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


# ══════════════════════════════════════════════════════════════════════
# Bollinger Bands
# ══════════════════════════════════════════════════════════════════════

def bollinger(
    close: pd.Series,
    period: int = 20,
    n_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands. Returns (upper, middle, lower).

    Middle = SMA(close, period)
    Upper  = Middle + n_std * rolling_std
    Lower  = Middle - n_std * rolling_std
    """
    middle = sma(close, period)
    # ddof=0 to match TradingView / most TA library conventions
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = middle + n_std * std
    lower = middle - n_std * std
    return upper, middle, lower


# ══════════════════════════════════════════════════════════════════════
# Stochastic Oscillator
# ══════════════════════════════════════════════════════════════════════

def stochastic_k(
    df: pd.DataFrame,
    period: int = 14,
    smooth: int = 3,
) -> pd.Series:
    """Stochastic %K smoothed.

        Raw %K = 100 * (C - L_n) / (H_n - L_n)
        %K     = SMA(raw_%K, smooth)
    """
    if period < 1 or smooth < 1:
        raise ValueError(f"period and smooth must be >= 1")

    lowest_low = df["low"].rolling(window=period, min_periods=period).min()
    highest_high = df["high"].rolling(window=period, min_periods=period).max()

    range_ = (highest_high - lowest_low).replace(0, np.nan)
    k_raw = 100 * (df["close"] - lowest_low) / range_
    return k_raw.rolling(window=smooth, min_periods=smooth).mean()


# ══════════════════════════════════════════════════════════════════════
# Annotate DataFrame with all indicators
# ══════════════════════════════════════════════════════════════════════

def annotate(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicator columns the FeatureBuilder will need in one pass.

    Input DataFrame must have columns: open, high, low, close, volume
    ('spread' is optional and preserved if present).

    Returns a NEW DataFrame (does not mutate input).
    """
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {missing}")

    out = df.copy()

    # EMAs
    out["ema_50"] = ema(df["close"], 50)
    out["ema_200"] = ema(df["close"], 200)

    # ATR at three horizons
    out["atr_5"] = atr(df, 5)
    out["atr_14"] = atr(df, 14)
    out["atr_20"] = atr(df, 20)

    # Oscillators
    out["rsi_14"] = rsi(df["close"], 14)
    out["adx_14"] = adx(df, 14)
    out["stoch_k_14"] = stochastic_k(df, 14, 3)

    # MACD
    _, _, macd_hist = macd(df["close"], 12, 26, 9)
    out["macd_hist"] = macd_hist

    # Bollinger
    bb_u, bb_m, bb_l = bollinger(df["close"], 20, 2.0)
    out["bb_upper_20"] = bb_u
    out["bb_middle_20"] = bb_m
    out["bb_lower_20"] = bb_l

    # Volume features (only if volume present)
    if "volume" in df.columns:
        out["volume_ma_20"] = sma(df["volume"].astype("float64"), 20)
        out["volume_std_20"] = df["volume"].astype("float64").rolling(
            window=20, min_periods=20
        ).std(ddof=0)

    return out
