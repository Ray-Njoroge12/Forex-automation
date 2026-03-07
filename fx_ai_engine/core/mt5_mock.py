from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

TIMEFRAME_M15 = 15
TIMEFRAME_H1 = 16385
ORDER_TYPE_BUY = 0
SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_FULL = 4


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
    currency: str = "USD"
    leverage: int = 100
    trade_allowed: bool = True


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
    time: int = 1_772_790_400


@dataclass
class _SymbolInfo:
    trade_mode: int = SYMBOL_TRADE_MODE_FULL
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    trade_contract_size: float = 100000.0
    trade_tick_value: float = 0.1
    trade_tick_size: float = 0.00001
    point: float = 0.00001
    digits: int = 5
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    time: int = 1_772_790_400


def symbol_info_tick(_symbol: str) -> _Tick:
    return _Tick()


def symbol_info(_symbol: str) -> _SymbolInfo:
    return _SymbolInfo()


def order_calc_margin(_order_type: int, _symbol: str, volume: float, price: float) -> float:
    return max(volume * price * 10.0, 0.01)
