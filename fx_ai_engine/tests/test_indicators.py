from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.indicators import calculate_adx, calculate_atr, calculate_ema, calculate_rsi


def _load_fixture() -> pd.DataFrame:
    path = Path(__file__).parent / "fixtures" / "ohlc_fixture.csv"
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df.set_index("time", inplace=True)
    return df


def test_ema_shape_and_warmup() -> None:
    df = _load_fixture()
    ema = calculate_ema(df["close"], period=5)
    assert len(ema) == len(df)
    assert ema.iloc[:4].isna().all()
    assert not pd.isna(ema.iloc[-1])


def test_atr_shape_and_positive_values() -> None:
    df = _load_fixture()
    atr = calculate_atr(df, period=14)
    assert len(atr) == len(df)
    assert atr.iloc[:13].isna().all()
    assert (atr.dropna() > 0).all()


def test_rsi_bounds_and_warmup() -> None:
    df = _load_fixture()
    rsi = calculate_rsi(df["close"], period=14)
    assert len(rsi) == len(df)
    assert rsi.iloc[:13].isna().all()
    valid = rsi.dropna()
    assert ((valid >= 0) & (valid <= 100)).all()


def test_adx_shape_warmup_and_bounds() -> None:
    # period=5 → warmup = 2*5-1 = 9 rows; fixture has 20 rows → 11 valid values
    df = _load_fixture()
    adx = calculate_adx(df, period=5)
    assert len(adx) == len(df)
    assert adx.iloc[:9].isna().all()
    assert not pd.isna(adx.iloc[9])
    valid = adx.dropna()
    assert ((valid >= 0) & (valid <= 100)).all()


def test_deterministic_output_last_values() -> None:
    df = _load_fixture()
    ema_last = float(calculate_ema(df["close"], period=5).iloc[-1])
    atr_last = float(calculate_atr(df, period=14).iloc[-1])
    rsi_last = float(calculate_rsi(df["close"], period=14).iloc[-1])
    adx_last = float(calculate_adx(df, period=5).iloc[-1])

    assert round(ema_last, 6) == 1.106442
    assert round(atr_last, 6) == 0.001093
    assert round(rsi_last, 6) == 89.246978
    assert round(adx_last, 6) == 100.0
