from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

TIMEFRAME_M15 = 15
TIMEFRAME_H1 = 16385


_last_error = (0, "OK")


def last_error() -> tuple[int, str]:
    return _last_error


def initialize(*_args: Any, **_kwargs: Any) -> bool:
    return True


def shutdown() -> None:
    return None


@dataclass
class _AccountInfo:
    balance: float = 10000.0
    equity: float = 10000.0
    margin_free: float = 10000.0


def account_info() -> _AccountInfo:
    return _AccountInfo()


@dataclass
class _Position:
    profit: float = 0.0


def positions_get() -> list[_Position]:
    return []


def symbol_select(_symbol: str, _enable: bool) -> bool:
    return True


def copy_rates_from_pos(_symbol: str, _timeframe: int, _start: int, count: int) -> Iterable[dict[str, Any]]:
    now = int(time.time())
    rates = []
    base = 1.1000
    for i in range(count):
        ts = now - (count - i) * 60
        open_ = base + i * 0.0001
        close = open_ + 0.00005
        high = max(open_, close) + 0.0001
        low = min(open_, close) - 0.0001
        rates.append(
            {
                "time": ts,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "tick_volume": 0,
                "spread": 0,
                "real_volume": 0,
            }
        )
    return rates


@dataclass
class _Tick:
    bid: float = 1.1000
    ask: float = 1.1002


def symbol_info_tick(_symbol: str) -> _Tick:
    return _Tick()
