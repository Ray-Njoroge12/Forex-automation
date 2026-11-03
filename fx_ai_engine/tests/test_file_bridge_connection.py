"""Contract tests for the EA file-bridge connection (ADR 0001, D4)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.file_bridge_connection import FileBridgeConnection

NOW = datetime(2026, 11, 3, 14, 7, 30, tzinfo=timezone.utc)
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"]
TF_SECONDS = {"M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_closed(tf: str) -> int:
    step = TF_SECONDS[tf]
    current_open = int(NOW.timestamp()) // step * step
    return current_open - step


def _bars(tf: str, count: int = 5) -> list[list[float]]:
    last = _last_closed(tf)
    rows = []
    for i in range(count):
        t = last - (count - 1 - i) * TF_SECONDS[tf]
        rows.append([t, 1.1000, 1.1010, 1.0990, 1.1005, 100 + i, 12])
    return rows


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def bridge(tmp_path: Path) -> Path:
    market = tmp_path / "market"
    feedback = tmp_path / "feedback"
    status = {
        "schema_version": 1,
        "generated_at": _iso(NOW - timedelta(seconds=3)),
        "ea_version": "test",
        "magic": 20261103,
        "server_utc_offset_seconds": 7200,
        "server_offset_valid": True,
        "terminal_connected": True,
        "account_is_demo": True,
        "login": 5058284077,
        "server": "MetaQuotes-Demo",
        "latest_closed_bars": {s: {tf: _last_closed(tf) for tf in TF_SECONDS} for s in SYMBOLS},
    }
    _write(market / "status.json", status)
    for sym in SYMBOLS:
        for tf in TF_SECONDS:
            _write(market / f"bars_{sym}_{tf}.json", {
                "schema_version": 1, "symbol": sym, "timeframe": tf,
                "generated_at": _iso(NOW - timedelta(minutes=7)),
                "server_utc_offset_seconds": 7200,
                "columns": ["time_utc", "open", "high", "low", "close", "tick_volume", "spread_points"],
                "bars": _bars(tf),
            })
    _write(feedback / "account_snapshot.json", {
        "timestamp": _iso(NOW - timedelta(seconds=4)),
        "account_is_demo": True, "account_authenticated": True, "terminal_connected": True,
        "login": 5058284077, "server": "MetaQuotes-Demo", "currency": "USD",
        "balance": 100000.0, "equity": 100050.0, "margin_free": 99000.0,
        "open_positions_count": 1, "floating_pnl": 50.0, "open_symbols": ["EURUSD"],
        "open_usd_exposure_count": 1, "management_state_restored": True,
        "managed_positions_count": 1, "managed_position_tickets": [111], "unmanaged_position_tickets": [],
    })
    _write(feedback / "market_quotes.json", {
        "schema_version": 1, "timestamp": _iso(NOW - timedelta(seconds=2)),
        "account_is_demo": True, "account_authenticated": True, "terminal_connected": True,
        "quotes": [{"symbol": "EURUSD", "bid": 1.10000, "ask": 1.10012, "tick_age_seconds": 1}],
    })
    _write(market / "symbols.json", {
        "schema_version": 1, "generated_at": _iso(NOW - timedelta(seconds=20)),
        "symbols": {"EURUSD": {"digits": 5, "point": 0.00001, "tick_size": 0.00001, "tick_value": 1.0,
                               "contract_size": 100000, "volume_min": 0.01, "volume_step": 0.01,
                               "volume_max": 500, "stops_level": 0, "freeze_level": 0,
                               "filling_mode": 1, "trade_mode": 4, "swap_long": -1, "swap_short": 0.5}},
    })
    open_ms = int((NOW - timedelta(hours=2)).timestamp() * 1000)
    close_ms = int((NOW - timedelta(hours=1)).timestamp() * 1000)
    _write(market / "deals.json", {
        "schema_version": 1, "generated_at": _iso(NOW - timedelta(seconds=10)),
        "server_utc_offset_seconds": 7200, "lookback_days": 30,
        "deals": [
            {"ticket": 2, "order": 20, "position_id": 999, "time_msc": close_ms, "type": 1, "entry": 1,
             "symbol": "EURUSD", "volume": 0.1, "price": 1.1050, "profit": 50.0, "commission": -0.7,
             "swap": 0.0, "fee": 0.0, "magic": 20261103, "comment": ""},
            {"ticket": 1, "order": 10, "position_id": 999, "time_msc": open_ms, "type": 0, "entry": 0,
             "symbol": "EURUSD", "volume": 0.1, "price": 1.1000, "profit": 0.0, "commission": -0.7,
             "swap": 0.0, "fee": 0.0, "magic": 20261103, "comment": "AI_TEST_1"},
        ],
    })
    return tmp_path


def _conn(path: Path, now: datetime = NOW) -> FileBridgeConnection:
    return FileBridgeConnection(path, clock=lambda: now)


def test_connect_reads_account_identity(bridge):
    conn = _conn(bridge)
    assert conn.connect(retry_count=1) is True
    assert conn.login == 5058284077
    assert conn.server == "MetaQuotes-Demo"
    assert conn.is_demo_account() is True


def test_connect_fails_closed_on_stale_status(bridge):
    conn = _conn(bridge, NOW + timedelta(minutes=5))
    assert conn.connect(retry_count=1) is False
    assert conn.last_error.code == "FILE_BRIDGE_NOT_READY"


def test_fetch_ohlc_returns_mt5_shaped_frame(bridge):
    conn = _conn(bridge)
    df = conn.fetch_ohlc_data("EURUSD", 15, 3)
    assert list(df.columns) == ["open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
    assert len(df) == 3
    assert str(df.index.tz) == "UTC"
    assert int(df.index[-1].timestamp()) == _last_closed("M15")
    assert df.index.is_monotonic_increasing


def test_fetch_ohlc_fails_closed_when_export_lags_terminal(bridge):
    status_path = bridge / "market" / "status.json"
    status = json.loads(status_path.read_text())
    status["latest_closed_bars"]["EURUSD"]["M15"] += 900
    status_path.write_text(json.dumps(status))
    df = _conn(bridge).fetch_ohlc_data("EURUSD", 15, 3)
    assert df.empty
    assert "lags the terminal" in df.attrs["error"]["message"]


def test_fetch_ohlc_rejects_inconsistent_ohlc(bridge):
    path = bridge / "market" / "bars_EURUSD_H1.json"
    payload = json.loads(path.read_text())
    payload["bars"][1][2] = 1.0  # high below open/close
    path.write_text(json.dumps(payload))
    df = _conn(bridge).fetch_ohlc_data("EURUSD", 16385, 5)
    assert df.empty
    assert "inconsistent OHLC" in df.attrs["error"]["message"]


def test_fetch_ohlc_rejects_unsupported_timeframe(bridge):
    df = _conn(bridge).fetch_ohlc_data("EURUSD", 1, 5)
    assert df.empty
    assert df.attrs["error"]["code"] == "FILE_BRIDGE_TIMEFRAME_UNSUPPORTED"


def test_live_spread_from_quote_snapshot(bridge):
    conn = _conn(bridge)
    assert conn.get_live_spread("EURUSD") == pytest.approx(0.00012)
    assert conn.get_live_spread("GBPUSD") is None


def test_stale_account_snapshot_returns_error(bridge):
    conn = _conn(bridge, NOW + timedelta(seconds=40))
    snapshot = conn.get_account_snapshot()
    assert "error" in snapshot
    assert conn.get_open_position_tickets() is None


def test_open_position_tickets_require_consistent_counts(bridge):
    conn = _conn(bridge)
    assert conn.get_open_position_tickets() == [111]
    path = bridge / "feedback" / "account_snapshot.json"
    payload = json.loads(path.read_text())
    payload["open_positions_count"] = 2
    path.write_text(json.dumps(payload))
    assert _conn(bridge).get_open_position_tickets() is None


def test_position_history_summary_from_deal_export(bridge):
    conn = _conn(bridge)
    summary = conn.get_position_history_summary(999)
    assert summary is not None
    assert summary["status"] == "CLOSED_WIN"
    assert summary["direction"] == "BUY"
    assert summary["profit_loss"] == pytest.approx(49.3)
    assert summary["trade_id"] == "AI_TEST_1"
    by_trade = conn.get_trade_history_summary("AI_TEST_1")
    assert by_trade["position_ticket"] == 999


def test_symbol_execution_contract(bridge):
    contract = _conn(bridge).get_symbol_execution_contract("EURUSD")
    assert contract.min_lot == 0.01
    assert contract.lot_step == 0.01
    assert _conn(bridge).get_symbol_execution_contract("GBPUSD") is None


def test_trade_feasibility_uses_exported_contract(bridge):
    decision = _conn(bridge).evaluate_trade_feasibility("EURUSD", 0.032, 20.0)
    assert decision.can_assess is True
    assert decision.approved is True
    assert decision.estimated_lot == pytest.approx(16.0)
