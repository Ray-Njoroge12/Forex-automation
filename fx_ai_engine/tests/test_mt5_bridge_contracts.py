from __future__ import annotations

import types

from core.mt5_bridge import MT5Connection


class _FailingInitMT5:
    def initialize(self, login: int, server: str, password: str) -> bool:
        return False

    def last_error(self):
        return (10001, "init failed")


class _SymbolUnavailableMT5:
    TIMEFRAME_M15 = 15

    def initialize(self, login: int, server: str, password: str) -> bool:
        return True

    def last_error(self):
        return (5001, "symbol unavailable")

    def account_info(self):
        return types.SimpleNamespace(balance=1000.0, equity=1000.0, margin_free=900.0)

    def positions_get(self):
        return []

    def symbol_select(self, symbol: str, select: bool) -> bool:
        return False

    def shutdown(self) -> None:
        return None


def test_connect_failure_sets_explicit_error(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _FailingInitMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    ok = bridge.connect()

    assert ok is False
    assert bridge.last_error is not None
    assert bridge.last_error.code == "MT5_INIT_FAILED"


def test_ohlc_failure_sets_error_attr(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _SymbolUnavailableMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    df = bridge.fetch_ohlc_data("EURUSD", timeframe=15, num_candles=10)

    assert df.empty
    assert "error" in df.attrs
    assert df.attrs["error"]["code"] == "SYMBOL_SELECT_FAILED"
