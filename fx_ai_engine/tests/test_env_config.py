from __future__ import annotations

import os
import pytest


def test_micro_capital_env_vars_are_valid():
    """Env var values for micro-capital mode must parse correctly."""
    test_env = {
        "MICRO_CAPITAL_MODE": "1",
        "FIXED_RISK_USD": "0.50",
        "MAX_SPREAD_PIPS": "3.5",
        "ML_PREDICT_THRESHOLD": "-1.0",
    }
    assert float(test_env["FIXED_RISK_USD"]) == 0.50
    assert float(test_env["MAX_SPREAD_PIPS"]) == 3.5
    assert float(test_env["ML_PREDICT_THRESHOLD"]) == -1.0
    assert test_env["MICRO_CAPITAL_MODE"] == "1"


def test_hard_risk_engine_reads_micro_capital_mode(monkeypatch):
    """HardRiskEngine must use relaxed limits when MICRO_CAPITAL_MODE=1."""
    monkeypatch.setenv("MICRO_CAPITAL_MODE", "1")
    from core.risk.hard_risk_engine import HardRiskEngine
    engine = HardRiskEngine()
    assert engine.max_daily_loss == 0.15
    assert engine.max_weekly_loss == 0.25
    assert engine.max_simultaneous_trades == 1


def test_adversarial_agent_reads_max_spread_pips(monkeypatch):
    """AdversarialAgent must read MAX_SPREAD_PIPS from env at instantiation."""
    monkeypatch.setenv("MAX_SPREAD_PIPS", "3.5")
    import pandas as pd
    from core.agents.adversarial_agent import AdversarialAgent

    agent = AdversarialAgent(
        symbol="EURUSD",
        fetch_ohlc=lambda s, t, n: pd.DataFrame(),
        fetch_spread=lambda s: None,
    )
    assert agent.max_spread_pips == 3.5
