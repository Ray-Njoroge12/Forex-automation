"""Small replay datasets built from the tracked live-bars fixture (no untracked CSVs needed)."""
from __future__ import annotations

from pathlib import Path

from backtesting.unified_replay import load_bridge_bar_exports

LIVE_BARS = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
FIXTURE_SYMBOLS = ("GBPUSD", "AUDUSD")


def write_fixture_csvs(dest: Path, symbols: tuple[str, ...] = FIXTURE_SYMBOLS) -> Path:
    """Write ``SYMBOL_TF.csv`` files readable by ``load_csv_frames`` into *dest* and return it."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    frames = load_bridge_bar_exports(LIVE_BARS, list(symbols))
    for symbol, by_tf in frames.items():
        for tf, frame in by_tf.items():
            out = frame.reset_index().rename(columns={"index": "time"})
            out["time"] = out["time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            out[["time", "open", "high", "low", "close", "volume", "spread_points"]].to_csv(
                dest / f"{symbol}_{tf}.csv", index=False
            )
    return dest
