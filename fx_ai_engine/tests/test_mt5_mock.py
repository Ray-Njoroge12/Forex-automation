from __future__ import annotations

import importlib
import json

from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.bridge_utils import get_mock_runtime_state_path
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15

def test_mt5_mock_connection(monkeypatch):
    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.delenv("MT5_MOCK_SCENARIO", raising=False)

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


def test_mt5_mock_default_scenario_preserves_monotonic_uptrend(monkeypatch):
    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.delenv("MT5_MOCK_SCENARIO", raising=False)

    import core.mt5_bridge as mt5_bridge

    importlib.reload(mt5_bridge)

    conn = mt5_bridge.MT5Connection(123, "password", "server")
    assert conn.connect() is True

    bars = conn.fetch_ohlc_data("EURUSD", TIMEFRAME_M15, 10)
    closes = bars["close"].tolist()

    assert all(curr > prev for prev, curr in zip(closes, closes[1:]))


def test_mt5_mock_signal_ready_scenario_generates_buy_signal(monkeypatch):
    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setenv("MT5_MOCK_SCENARIO", "signal_ready_eurusd")

    import core.mt5_bridge as mt5_bridge

    importlib.reload(mt5_bridge)

    conn = mt5_bridge.MT5Connection(123, "password", "server")
    assert conn.connect() is True

    regime = RegimeAgent("EURUSD", conn.fetch_ohlc_data).evaluate(TIMEFRAME_H1)
    technical = TechnicalAgent("EURUSD", conn.fetch_ohlc_data, conn.get_live_spread).evaluate(
        regime,
        TIMEFRAME_M15,
        TIMEFRAME_H1,
    )

    assert regime.regime == "TRENDING_BULL"
    assert technical is not None
    assert technical.direction == "BUY"
    assert technical.reason_code == "TECH_CONFIRMED_BUY"


def test_mt5_mock_account_snapshot_uses_persisted_runtime_state(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setenv("MT5_MOCK_BRIDGE_PATH", str(tmp_path / "bridge"))

    import core.mt5_bridge as mt5_bridge

    importlib.reload(mt5_bridge)

    bridge_path = tmp_path / "bridge"
    bridge_path.mkdir(parents=True, exist_ok=True)
    get_mock_runtime_state_path(bridge_path).write_text(
        json.dumps({"balance": 9876.54, "equity": 9850.12, "outcome_index": 4}),
        encoding="utf-8",
    )

    conn = mt5_bridge.MT5Connection(123, "password", "server")
    assert conn.connect() is True

    snapshot = conn.get_account_snapshot()

    assert snapshot["balance"] == 9876.54
    assert snapshot["equity"] == 9850.12
    assert snapshot["margin_free"] == 9850.12


def test_mt5_mock_preserve_10_approval_profile_passes_startup_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    monkeypatch.setenv("MT5_MOCK_APPROVAL_PROFILE", "preserve_10_ready")
    monkeypatch.setenv("MT5_MOCK_BRIDGE_PATH", str(tmp_path / "preserve10_bridge"))

    import core.mt5_bridge as mt5_bridge
    import main as main_mod
    from config_microcapital import get_policy_config

    importlib.reload(mt5_bridge)

    conn = mt5_bridge.MT5Connection(123, "password", "server")
    assert conn.connect() is True

    decision = main_mod.evaluate_preserve_10_startup_approval(
        conn,
        policy=get_policy_config(mode_id="preserve_10"),
        env={main_mod.PRESERVE_10_COMMISSION_PER_LOT_ENV: "0"},
    )
    snapshot = conn.get_account_snapshot()

    assert decision.approved is True
    assert decision.reason_code == "PRESERVE_10_STARTUP_APPROVED"
    assert snapshot["balance"] == 10.0
    assert snapshot["equity"] == 10.0
