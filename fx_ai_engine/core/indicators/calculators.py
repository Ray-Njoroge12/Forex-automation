from __future__ import annotations

import pandas as pd


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """Deterministic EMA using pandas EWM."""
    if period <= 0:
        raise ValueError("EMA period must be > 0")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range with rolling mean smoothing.

    Warmup policy: first `period-1` rows are NaN.
    """
    if period <= 0:
        raise ValueError("ATR period must be > 0")
    required = {"high", "low", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"ATR requires columns: {required}")

    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.rolling(window=period, min_periods=period).mean()


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index using Wilder-style EWM smoothing.

    Warmup policy: first `2*period - 1` rows are NaN (two Wilder passes).
    """
    if period <= 0:
        raise ValueError("ADX period must be > 0")
    required = {"high", "low", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"ADX requires columns: {required}")

    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = pd.Series(
        ((up_move > down_move) & (up_move > 0)) * up_move,
        index=df.index,
    )
    minus_dm = pd.Series(
        ((down_move > up_move) & (down_move > 0)) * down_move,
        index=df.index,
    )

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    alpha = 1 / period
    smoothed_plus_dm = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    smoothed_tr = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    di_plus = 100 * smoothed_plus_dm / smoothed_tr
    di_minus = 100 * smoothed_minus_dm / smoothed_tr

    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus)
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    # Enforce warmup: the second EWM pass needs another period rows after the
    # first period rows warm up, so NaN the first 2*period - 1 rows.
    warmup = 2 * period - 1
    adx.iloc[:warmup] = float("nan")

    # Clamp to [0, 100] to guard against floating-point overshoot.
    return adx.clip(lower=0.0, upper=100.0)


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI using Wilder-style EWM smoothing.

    Warmup policy: first `period-1` rows are NaN.
    """
    if period <= 0:
        raise ValueError("RSI period must be > 0")

    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi
