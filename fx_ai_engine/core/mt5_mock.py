from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

TIMEFRAME_M15 = 15
TIMEFRAME_H1 = 16385
TIMEFRAME_H4 = 16388
ORDER_TYPE_BUY = 0
SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_FULL = 4
DEFAULT_SCENARIO = "steady_uptrend"
SIGNAL_READY_EURUSD_SCENARIO = "signal_ready_eurusd"
DEFAULT_APPROVAL_PROFILE = "standard"
PRESERVE_10_READY_APPROVAL_PROFILE = "preserve_10_ready"


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


def _approval_profile() -> str:
    return os.getenv("MT5_MOCK_APPROVAL_PROFILE", DEFAULT_APPROVAL_PROFILE).strip().lower() or DEFAULT_APPROVAL_PROFILE


def account_info() -> _AccountInfo:
    if _approval_profile() == PRESERVE_10_READY_APPROVAL_PROFILE:
        return _AccountInfo(
            balance=1000.0,
            equity=1000.0,
            margin_free=1000.0,
            currency="USC",
            leverage=500,
            trade_allowed=True,
        )
    return _AccountInfo()


@dataclass
class _Position:
    profit: float = 0.0


def positions_get() -> list[_Position]:
    return []


def symbol_select(_symbol: str, _enable: bool) -> bool:
    return True


def _seconds_per_bar(timeframe: int) -> int:
    return {
        TIMEFRAME_M15: 15 * 60,
        TIMEFRAME_H1: 60 * 60,
        TIMEFRAME_H4: 4 * 60 * 60,
    }.get(timeframe, 60)


def _rates_from_closes(closes: list[float], *, step_seconds: int, now: int, wick: float = 0.0001, final_wick: float | None = None) -> list[dict[str, Any]]:
    rates = []
    count = len(closes)
    for i, close in enumerate(closes):
        ts = now - (count - i) * step_seconds
        open_ = closes[i - 1] if i > 0 else close - 0.00005
        low_wick = final_wick if final_wick is not None and i >= count - 5 else wick
        high = max(open_, close) + 0.0001
        low = min(open_, close) - low_wick
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


def _steady_uptrend_rates(timeframe: int, count: int, now: int) -> list[dict[str, Any]]:
    base = 1.1000
    closes = [round(base + i * 0.0001 + 0.00005, 6) for i in range(count)]
    return _rates_from_closes(closes, step_seconds=_seconds_per_bar(timeframe), now=now)


def _signal_ready_eurusd_rates(symbol: str, timeframe: int, count: int, now: int) -> list[dict[str, Any]]:
    if symbol != "EURUSD":
        return _steady_uptrend_rates(timeframe, count, now)

    if timeframe in {TIMEFRAME_H1, TIMEFRAME_H4}:
        closes = [round(1.1000 + i * 0.0001 + 0.00003 * math.sin(i / 10.0), 6) for i in range(count)]
        return _rates_from_closes(closes, step_seconds=_seconds_per_bar(timeframe), now=now)

    if timeframe == TIMEFRAME_M15:
        price = 1.1000
        closes: list[float] = []
        for i in range(count):
            if i < count - 23:
                price += 0.00002 + 0.000015 * math.sin(i / 7.0)
            elif i < count - 3:
                price -= 0.00002
            else:
                price += 0.00002
            closes.append(round(price, 6))
        return _rates_from_closes(
            closes,
            step_seconds=_seconds_per_bar(timeframe),
            now=now,
            final_wick=0.0002,
        )

    return _steady_uptrend_rates(timeframe, count, now)


def copy_rates_from_pos(symbol: str, timeframe: int, _start: int, count: int) -> Iterable[dict[str, Any]]:
    now = int(time.time())
    scenario = os.getenv("MT5_MOCK_SCENARIO", DEFAULT_SCENARIO).strip().lower() or DEFAULT_SCENARIO
    if scenario == SIGNAL_READY_EURUSD_SCENARIO:
        return _signal_ready_eurusd_rates(symbol, timeframe, count, now)
    return _steady_uptrend_rates(timeframe, count, now)


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


def symbol_info_tick(symbol: str) -> _Tick:
    if _approval_profile() == PRESERVE_10_READY_APPROVAL_PROFILE:
        now = int(time.time())
        if symbol.endswith("JPY"):
            return _Tick(bid=150.000, ask=150.020, time=now)
        return _Tick(bid=1.10000, ask=1.10008, time=now)
    return _Tick()


def symbol_info(symbol: str) -> _SymbolInfo:
    if _approval_profile() == PRESERVE_10_READY_APPROVAL_PROFILE:
        if symbol.endswith("JPY"):
            return _SymbolInfo(
                volume_min=0.01,
                volume_step=0.01,
                volume_max=10.0,
                trade_contract_size=100000.0,
                trade_tick_value=1.0,
                trade_tick_size=0.001,
                point=0.001,
                digits=3,
                time=int(time.time()),
            )
        return _SymbolInfo(
            volume_min=0.01,
            volume_step=0.01,
            volume_max=10.0,
            trade_contract_size=100000.0,
            trade_tick_value=1.0,
            trade_tick_size=0.00001,
            point=0.00001,
            digits=5,
            time=int(time.time()),
        )
    return _SymbolInfo()


def order_calc_margin(_order_type: int, _symbol: str, volume: float, price: float) -> float:
    if _approval_profile() == PRESERVE_10_READY_APPROVAL_PROFILE:
        return max((volume / 0.01) * 25.0, 0.01)
    return max(volume * price * 10.0, 0.01)
