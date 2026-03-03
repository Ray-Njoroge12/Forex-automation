from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_ohlc_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "time" not in df.columns:
        raise ValueError("CSV missing required column: time")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            raise ValueError(f"CSV missing required column: {col}")

    if "volume" not in df.columns:
        df["volume"] = 0.0
    return df
