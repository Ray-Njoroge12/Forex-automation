"""C1 Python half: broker symbol suffix and EA profile fields on the file bridge."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from core.file_bridge_connection import FileBridgeConnection
from tests.bridge_fixtures import SUFFIXED_PROFILE, build_full_usd_bridge, convert_bridge_to_suffixed

TF_M15 = 15


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc)


def _usd(tmp_path: Path, now: datetime) -> Path:
    return build_full_usd_bridge(tmp_path / "usd", now, bar_count=5)


def _conn(path: Path, now: datetime) -> FileBridgeConnection:
    return FileBridgeConnection(path, clock=lambda: now)


def _edit(path: Path, relative: str, **changes) -> None:
    target = path / relative
    data = json.loads(target.read_text())
    data.update(changes)
    target.write_text(json.dumps(data))


def _readers(conn: FileBridgeConnection) -> dict:
    assert conn.connect(retry_count=1), conn.last_error
    return {
        "bars": conn.fetch_ohlc_data("EURUSD", TF_M15, 5),
        "spread": conn.get_live_spread("EURUSD"),
        "contract": conn.get_symbol_execution_contract("EURUSD"),
        "snapshot": conn.get_account_snapshot(),
        "tickets": conn.get_open_position_tickets(),
        "position": conn.get_position_history_summary(999),
        "trade": conn.get_trade_history_summary("AI_TEST_1"),
    }


def _assert_same(left: dict, right: dict) -> None:
    pd.testing.assert_frame_equal(left["bars"], right["bars"])
    assert left["bars"].attrs == right["bars"].attrs
    for key in ("spread", "contract", "snapshot", "tickets", "position", "trade"):
        assert left[key] == right[key], key


def test_suffixed_bridge_is_read_with_canonical_names(tmp_path, now):
    suffixed = convert_bridge_to_suffixed(_usd(tmp_path, now), tmp_path / "sfx", "c", profile=SUFFIXED_PROFILE)
    results = _readers(_conn(suffixed, now))
    plain = _readers(_conn(_usd(tmp_path / "again", now), now))
    assert not results["bars"].empty and results["bars"].attrs.get("error") is None
    assert results["spread"] is not None
    assert results["contract"].symbol == "EURUSD"
    assert results["snapshot"]["balance"] == 100000.0
    assert results["position"] is not None and results["trade"] is not None
    _assert_same(results, plain)
    symbols = json.loads((suffixed / "market" / "symbols.json").read_text())["symbols"]
    assert symbols["EURUSD"]["broker_symbol"] == "EURUSDc"
    assert (suffixed / "market" / "bars_EURUSD_M15.json").exists()
    assert json.loads((suffixed / "market" / "bars_EURUSD_M15.json").read_text())["symbol"] == "EURUSD"


def test_empty_suffix_bridge_matches_plain_bridge(tmp_path, now):
    plain_dir = _usd(tmp_path / "p", now)
    empty = convert_bridge_to_suffixed(_usd(tmp_path / "e", now), tmp_path / "empty", "")
    _assert_same(_readers(_conn(plain_dir, now)), _readers(_conn(empty, now)))


def test_absent_fields_behave_as_before(tmp_path, now):
    conn = _conn(_usd(tmp_path, now), now)
    assert conn.connect(retry_count=1) is True
    assert conn.get_ea_profile() == {
        "ea_version": "test", "symbol_suffix": None, "symbols_ok": None, "symbol_errors": None,
        "account_margin_mode": None, "account_currency": None, "max_spread_pips_input": None, "live_guard": None,
    }


def test_get_ea_profile_reports_values(tmp_path, now):
    suffixed = convert_bridge_to_suffixed(_usd(tmp_path, now), tmp_path / "sfx", "c", profile=SUFFIXED_PROFILE)
    profile = _conn(suffixed, now).get_ea_profile()
    assert profile == {
        "ea_version": "test", "symbol_suffix": "c", "symbols_ok": True, "symbol_errors": [],
        "account_margin_mode": "HEDGING", "account_currency": "USD", "max_spread_pips_input": 2.0,
        "live_guard": {"enabled": False},
    }


def test_symbols_not_ok_fails_closed_with_symbol_list(tmp_path, now):
    bad = convert_bridge_to_suffixed(
        _usd(tmp_path, now), tmp_path / "bad", "c", symbols_ok=False, symbol_errors=("EURUSD", "GBPUSD"),
        profile=SUFFIXED_PROFILE,
    )
    conn = _conn(bad, now)
    assert conn.connect(retry_count=1) is False
    assert conn.last_error.code == "FILE_BRIDGE_SYMBOLS_NOT_OK"
    assert "EA could not resolve broker symbols (suffix='c'): EURUSD, GBPUSD" in conn.last_error.message

    fresh = _conn(bad, now)
    snapshot = fresh.get_account_snapshot()
    assert snapshot["error"]["code"] == "FILE_BRIDGE_SYMBOLS_NOT_OK"
    frame = fresh.fetch_ohlc_data("EURUSD", TF_M15, 5)
    assert frame.empty and frame.attrs["error"]["code"] == "FILE_BRIDGE_SYMBOLS_NOT_OK"
    assert fresh.get_open_position_tickets() is None and fresh.last_error.code == "FILE_BRIDGE_SYMBOLS_NOT_OK"
    assert fresh.is_demo_account() is None and fresh.last_error.code == "FILE_BRIDGE_SYMBOLS_NOT_OK"
    # The profile read does not apply the gate, so a checker can still report it.
    profile = fresh.get_ea_profile()
    assert profile["symbols_ok"] is False and profile["symbol_errors"] == ["EURUSD", "GBPUSD"]


def test_symbols_not_ok_without_errors_says_unspecified(tmp_path, now):
    bad = convert_bridge_to_suffixed(_usd(tmp_path, now), tmp_path / "bad", "", symbols_ok=False)
    conn = _conn(bad, now)
    assert conn.connect(retry_count=1) is False
    assert conn.last_error.code == "FILE_BRIDGE_SYMBOLS_NOT_OK"
    assert conn.last_error.message.endswith("(suffix=''): unspecified")


@pytest.mark.parametrize("changes", [
    {"symbol_suffix": 5},
    {"symbols_ok": "yes"},
    {"symbols_ok": 1},
    {"symbol_errors": "EURUSD"},
    {"symbol_errors": [1]},
    {"symbols_ok": True, "symbol_errors": ["EURUSD"]},
    {"account_currency": ""},
    {"account_currency": 5},
    {"max_spread_pips_input": -1},
    {"max_spread_pips_input": True},
    {"max_spread_pips_input": "2"},
    {"max_spread_pips_input": math.nan},
    {"live_guard": []},
    {"account_margin_mode": True},
    {"account_margin_mode": 1.5},
    {"account_margin_mode": []},
])
def test_malformed_profile_field_fails_closed(tmp_path, now, changes):
    bridge = _usd(tmp_path, now)
    _edit(bridge, "market/status.json", **changes)
    conn = _conn(bridge, now)
    assert conn.connect(retry_count=1) is False
    assert conn.last_error.code == "FILE_BRIDGE_STATUS_FIELD_INVALID"
    with pytest.raises(ValueError):
        _conn(bridge, now).get_ea_profile()


@pytest.mark.parametrize("changes", [
    {"symbol_suffix": ""}, {"account_margin_mode": 2}, {"account_margin_mode": "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING"},
    {"max_spread_pips_input": 0}, {"live_guard": {}},
])
def test_wellformed_profile_field_is_accepted(tmp_path, now, changes):
    bridge = _usd(tmp_path, now)
    _edit(bridge, "market/status.json", **changes)
    assert _conn(bridge, now).connect(retry_count=1) is True


def test_currency_mismatch_between_status_and_snapshot(tmp_path, now):
    bridge = _usd(tmp_path, now)
    _edit(bridge, "market/status.json", account_currency="USC")
    conn = _conn(bridge, now)
    assert conn.connect(retry_count=1) is True
    snapshot = conn.get_account_snapshot()
    assert snapshot["error"]["code"] == "FILE_BRIDGE_ACCOUNT_CURRENCY_MISMATCH"
    assert conn.get_open_position_tickets() is None
    assert conn.last_error.code == "FILE_BRIDGE_ACCOUNT_CURRENCY_MISMATCH"


def test_currency_match_is_case_insensitive(tmp_path, now):
    bridge = _usd(tmp_path, now)
    _edit(bridge, "market/status.json", account_currency="usd")
    assert "error" not in _conn(bridge, now).get_account_snapshot()


@pytest.mark.parametrize("value", ["", "  ", 5, None])
def test_invalid_broker_symbol_keeps_precise_code(tmp_path, now, value):
    bridge = _usd(tmp_path, now)
    path = bridge / "market" / "symbols.json"
    data = json.loads(path.read_text())
    data["symbols"]["EURUSD"]["broker_symbol"] = value
    path.write_text(json.dumps(data))
    conn = _conn(bridge, now)
    assert conn.get_symbol_execution_contract("EURUSD") is None
    assert conn.last_error.code == "FILE_BRIDGE_STATUS_FIELD_INVALID"
    assert conn.get_symbol_execution_contract("GBPUSD") is not None
