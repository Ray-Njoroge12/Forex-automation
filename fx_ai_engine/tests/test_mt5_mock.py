from __future__ import annotations

import importlib

def test_mt5_mock_connection(monkeypatch):
    monkeypatch.setenv("USE_MT5_MOCK", "1")

    import core.mt5_bridge as mt5_bridge

    importlib.reload(mt5_bridge)

    conn = mt5_bridge.MT5Connection(123, "password", "server")
    assert conn.connect() is True

    snapshot = conn.get_account_snapshot()
    assert isinstance(snapshot, dict)

    df = conn.fetch_ohlc_data("EURUSD", 15, 10)
    assert df is not None

    spread = conn.get_live_spread("EURUSD")
    assert spread is None or isinstance(spread, float)
