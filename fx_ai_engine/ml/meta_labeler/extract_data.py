"""Historical bar extraction from MetaTrader 5.

Pulls OHLC + volume (+ spread, if available) for each symbol/timeframe
combination and stores them as compressed Parquet files. A manifest
records what was extracted and when — this is what the training pipeline
reads to ensure reproducibility.

Why Parquet?
    - 10-20x smaller on disk than CSV
    - Typed columns (preserves int/float distinctions)
    - ~50x faster to load into pandas
    - Zstandard compression is fast on modern CPUs

Why a manifest?
    - Every training run reads from the manifest. If anyone re-extracts
      data with different parameters, the manifest changes and we know.
    - Reproducibility: a model trained on a specific manifest can be
      retrained from the same data.

MT5 is a lazy import (inside _require_mt5) so this module can be unit-tested
on Linux/macOS where MetaTrader5 is not available.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════

DEFAULT_SYMBOLS: tuple[str, ...] = (
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
)
DEFAULT_TIMEFRAMES: tuple[str, ...] = ("M15", "H1")
DEFAULT_YEARS_BACK: int = 5
DEFAULT_DATA_DIR = Path("data/historical")

# Columns we keep from the MT5 rates array.
# MT5 also returns 'real_volume' but it's often zero for forex.
_KEEP_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "spread")


@dataclass(frozen=True)
class ExtractionResult:
    """Result of extracting a single (symbol, timeframe) pair."""
    symbol: str
    timeframe: str
    rows: int
    start: pd.Timestamp
    end: pd.Timestamp
    path: Path
    extracted_at: datetime

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        d["path"] = str(self.path)
        d["extracted_at"] = self.extracted_at.isoformat()
        return d


# ══════════════════════════════════════════════════════════════════════
# MT5 helpers (lazy import)
# ══════════════════════════════════════════════════════════════════════

def _require_mt5():
    """Import MetaTrader5 or raise with a helpful message."""
    try:
        import MetaTrader5 as mt5  # type: ignore[import]
        return mt5
    except ImportError as e:
        raise RuntimeError(
            "MetaTrader5 package not available. "
            "Install on Windows with: pip install MetaTrader5\n"
            "This module cannot run on Linux/macOS."
        ) from e


def _tf_constants(mt5) -> dict[str, int]:
    """Lazy map of timeframe string → MT5 integer constant."""
    return {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }


# ══════════════════════════════════════════════════════════════════════
# Data transformation
# ══════════════════════════════════════════════════════════════════════

def bars_to_dataframe(rates) -> pd.DataFrame:
    """Convert an MT5 `rates` array (numpy structured array) to a typed
    DataFrame with a UTC DatetimeIndex.

    Handles:
        - empty arrays (returns empty DataFrame)
        - missing optional columns (spread)
        - volume rename (tick_volume → volume)
    """
    df = pd.DataFrame(rates)
    if df.empty:
        return df

    # MT5 'time' is Unix epoch in broker-server seconds — treat as UTC.
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time").sort_index()

    # Standardize column name: we prefer 'volume' over 'tick_volume'.
    if "tick_volume" in df.columns and "volume" not in df.columns:
        df = df.rename(columns={"tick_volume": "volume"})

    # Keep only columns we care about (some that might be present).
    keep = [c for c in _KEEP_COLUMNS if c in df.columns]
    df = df[keep]

    # Enforce types
    dtype_map: dict[str, str] = {}
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            dtype_map[col] = "float64"
    if "volume" in df.columns:
        dtype_map["volume"] = "int64"
    if "spread" in df.columns:
        dtype_map["spread"] = "int32"

    return df.astype(dtype_map)


# ══════════════════════════════════════════════════════════════════════
# MT5 lifecycle
# ══════════════════════════════════════════════════════════════════════

def initialize_mt5(path: Optional[str] = None, login: Optional[int] = None,
                    password: Optional[str] = None, server: Optional[str] = None) -> None:
    """Initialize MT5. Call once before extract_*() calls.

    Args:
        path: optional explicit path to terminal64.exe.
        login/password/server: optional credentials for explicit login.
            If omitted, MT5 uses the currently logged-in terminal.
    """
    mt5 = _require_mt5()
    kwargs: dict = {}
    if path is not None:
        kwargs["path"] = path
    if login is not None:
        kwargs["login"] = login
    if password is not None:
        kwargs["password"] = password
    if server is not None:
        kwargs["server"] = server

    ok = mt5.initialize(**kwargs)
    if not ok:
        err = mt5.last_error()
        raise RuntimeError(f"MT5 initialize() failed: {err}")
    logger.info("MT5 initialized. Version: %s", mt5.version())


def shutdown_mt5() -> None:
    """Shut down MT5. Safe to call even if mt5 unavailable (no-op)."""
    try:
        mt5 = _require_mt5()
        mt5.shutdown()
        logger.info("MT5 shut down")
    except RuntimeError:
        pass  # mt5 not installed — nothing to do


# ══════════════════════════════════════════════════════════════════════
# Extraction
# ══════════════════════════════════════════════════════════════════════

def extract_symbol(
    symbol: str,
    timeframe: str = "M15",
    years_back: int = DEFAULT_YEARS_BACK,
    *,
    end: Optional[datetime] = None,
) -> pd.DataFrame:
    """Pull historical bars for a single (symbol, timeframe).

    Does NOT initialize/shutdown MT5 — caller owns that lifecycle so
    multiple calls don't re-init for each one.

    Args:
        symbol: e.g. "EURUSD".
        timeframe: one of M1/M5/M15/M30/H1/H4/D1.
        years_back: how many years of history to pull (default 5).
        end: cutoff datetime (default: now UTC).

    Returns:
        Typed DataFrame with UTC DatetimeIndex.

    Raises:
        ValueError: unknown timeframe.
        RuntimeError: MT5 call failed, no data, or symbol not available.
    """
    mt5 = _require_mt5()
    tf_map = _tf_constants(mt5)
    if timeframe not in tf_map:
        raise ValueError(
            f"Unknown timeframe {timeframe!r}. Valid: {sorted(tf_map)}"
        )

    end_dt = end or datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=365 * years_back)

    # Must explicitly select the symbol so the server will stream its data.
    if not mt5.symbol_select(symbol, True):
        err = mt5.last_error()
        raise RuntimeError(
            f"symbol_select({symbol!r}) failed: {err}. "
            f"Is this symbol available on your broker?"
        )

    logger.info(
        "Extracting %s %s from %s to %s",
        symbol, timeframe, start_dt.isoformat(), end_dt.isoformat(),
    )
    rates = mt5.copy_rates_range(symbol, tf_map[timeframe], start_dt, end_dt)
    if rates is None or len(rates) == 0:
        err = mt5.last_error()
        raise RuntimeError(
            f"No data returned for {symbol} {timeframe}: {err}"
        )

    df = bars_to_dataframe(rates)
    logger.info(
        "Extracted %d bars for %s %s (%s → %s)",
        len(df), symbol, timeframe, df.index[0], df.index[-1],
    )
    return df


def save_parquet(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> Path:
    """Write a DataFrame to a Parquet file. Creates data_dir if needed."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{symbol}_{timeframe}.parquet"
    df.to_parquet(path, compression="zstd", index=True)
    return path


def load_parquet(
    symbol: str,
    timeframe: str,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> pd.DataFrame:
    """Load a previously-saved Parquet file. Raises if not found."""
    path = Path(data_dir) / f"{symbol}_{timeframe}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"No extracted data for {symbol} {timeframe} at {path}. "
            f"Run `python -m ml.meta_labeler.extract_data` first."
        )
    return pd.read_parquet(path)


def extract_and_save(
    symbol: str,
    timeframe: str,
    years_back: int = DEFAULT_YEARS_BACK,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> ExtractionResult:
    """Extract one (symbol, timeframe) and persist it. Returns summary.

    Requires MT5 to be initialized by the caller.
    """
    df = extract_symbol(symbol, timeframe, years_back)
    path = save_parquet(df, symbol, timeframe, data_dir)
    return ExtractionResult(
        symbol=symbol,
        timeframe=timeframe,
        rows=len(df),
        start=df.index[0],
        end=df.index[-1],
        path=path,
        extracted_at=datetime.now(timezone.utc),
    )


def extract_all(
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES,
    years_back: int = DEFAULT_YEARS_BACK,
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    mt5_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> dict[str, ExtractionResult]:
    """Extract every (symbol × timeframe) combination and save them.

    Writes a manifest.json summarising what was extracted. This manifest
    is the source of truth for reproducible training.
    """
    data_dir = Path(data_dir)
    initialize_mt5(path=mt5_path, login=login, password=password, server=server)
    try:
        manifest: dict[str, ExtractionResult] = {}
        for sym in symbols:
            for tf in timeframes:
                key = f"{sym}_{tf}"
                try:
                    res = extract_and_save(sym, tf, years_back, data_dir)
                    manifest[key] = res
                    logger.info(
                        "OK  %s: %d rows [%s → %s]",
                        key, res.rows, res.start, res.end,
                    )
                except Exception as exc:
                    logger.error("FAIL %s: %s", key, exc)
                    raise
        _write_manifest(manifest, data_dir)
        return manifest
    finally:
        shutdown_mt5()


def _write_manifest(
    manifest: dict[str, ExtractionResult],
    data_dir: Path,
) -> None:
    path = data_dir / "manifest.json"
    payload = {k: v.to_dict() for k, v in manifest.items()}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    logger.info("Manifest written to %s", path)


def load_manifest(data_dir: Path = DEFAULT_DATA_DIR) -> dict:
    """Load the manifest.json for a data directory. Returns {} if absent."""
    path = Path(data_dir) / "manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


# ══════════════════════════════════════════════════════════════════════
# CLI entry point
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s - %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Extract historical bars from MetaTrader 5 to Parquet.",
    )
    parser.add_argument(
        "--symbols", nargs="+", default=list(DEFAULT_SYMBOLS),
        help="Symbols to extract (default: the 6 majors).",
    )
    parser.add_argument(
        "--timeframes", nargs="+", default=list(DEFAULT_TIMEFRAMES),
        help="Timeframes to extract (default: M15 H1).",
    )
    parser.add_argument(
        "--years", type=int, default=DEFAULT_YEARS_BACK,
        help=f"Years of history to pull (default: {DEFAULT_YEARS_BACK}).",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_DATA_DIR,
        help=f"Output directory (default: {DEFAULT_DATA_DIR}).",
    )
    parser.add_argument("--mt5-path", default=None, help="Path to terminal64.exe")
    args = parser.parse_args()

    manifest = extract_all(
        symbols=tuple(args.symbols),
        timeframes=tuple(args.timeframes),
        years_back=args.years,
        data_dir=args.out,
        mt5_path=args.mt5_path,
    )
    print(f"\n=== Extracted {len(manifest)} datasets to {args.out} ===")
    for key, res in sorted(manifest.items()):
        print(f"  {key:20s} {res.rows:>8,} rows  [{res.start} → {res.end}]")
    print("\nManifest: ", args.out / "manifest.json")


if __name__ == "__main__":
    main()
