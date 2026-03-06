from __future__ import annotations

import pytest


def test_micro_capital_env_vars_are_valid(monkeypatch):
    """Env var parsing must work correctly for all micro-capital vars."""
    monkeypatch.setenv("FIXED_RISK_USD", "0.50")
    from core.risk.hard_risk_engine import _read_fixed_risk_usd
    assert _read_fixed_risk_usd() == 0.50

    monkeypatch.setenv("FIXED_RISK_USD", "invalid")
    assert _read_fixed_risk_usd() is None

    monkeypatch.delenv("FIXED_RISK_USD", raising=False)
    assert _read_fixed_risk_usd() is None


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
