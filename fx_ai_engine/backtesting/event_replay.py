from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

import pandas as pd


@dataclass(frozen=True)
class ReplayEvent:
    timestamp: pd.Timestamp
    symbol: str
    closed_bars: pd.DataFrame


@dataclass(frozen=True)
class ReplayCostModel:
    spread_pips: float
    slippage_pips: float = 0.0
    commission_per_lot_usd: float = 0.0

    def market_fill(self, *, symbol: str, direction: str, mid_price: float) -> float:
        pip = 0.01 if "JPY" in symbol else 0.0001
        half_spread = self.spread_pips * pip / 2.0
        slippage = self.slippage_pips * pip
        if direction == "BUY":
            return mid_price + half_spread + slippage
        if direction == "SELL":
            return mid_price - half_spread - slippage
        raise ValueError("direction must be BUY or SELL")

    def round_trip_commission(self, lot_size: float) -> float:
        return max(0.0, float(lot_size)) * max(0.0, self.commission_per_lot_usd)


class MultiSymbolEventReplay:
    """Chronologically emits completed-bar views for several symbols.

    Strategy/risk callbacks can use the same pure interfaces as live operation;
    the replay never exposes rows after the current event timestamp.
    """

    def __init__(self, frames: Mapping[str, pd.DataFrame]):
        self.frames = {symbol: frame.sort_index().copy() for symbol, frame in frames.items()}

    def events(self):
        timeline = sorted({timestamp for frame in self.frames.values() for timestamp in frame.index})
        for timestamp in timeline:
            for symbol in sorted(self.frames):
                frame = self.frames[symbol]
                if timestamp not in frame.index:
                    continue
                yield ReplayEvent(timestamp=pd.Timestamp(timestamp), symbol=symbol, closed_bars=frame.loc[:timestamp].copy())

    def run(self, on_event: Callable[[ReplayEvent], None]) -> None:
        for event in self.events():
            on_event(event)
