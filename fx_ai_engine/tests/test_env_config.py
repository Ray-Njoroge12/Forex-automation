from __future__ import annotations

import pytest


def _clear_policy_env(monkeypatch) -> None:
    for name in (
        "FX_POLICY_MODE",
        "MICRO_CAPITAL_MODE",
        "FIXED_RISK_USD",
        "MAX_SPREAD_PIPS",
        "ML_PREDICT_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)


def test_micro_capital_env_vars_are_valid(monkeypatch):
    """Legacy/preserve mode may still use explicit governed runtime overrides."""
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("MICRO_CAPITAL_MODE", "1")
    monkeypatch.setenv("FIXED_RISK_USD", "0.50")
    monkeypatch.setenv("MAX_SPREAD_PIPS", "3.5")
    monkeypatch.setenv("ML_PREDICT_THRESHOLD", "-1.0")
    from config_microcapital import (
        read_fixed_risk_usd,
        read_max_spread_pips,
        read_predict_threshold,
    )

    assert read_fixed_risk_usd() == 0.50
    assert read_max_spread_pips() == 3.5
    assert read_predict_threshold() == -1.0

    monkeypatch.setenv("FIXED_RISK_USD", "invalid")
    monkeypatch.setenv("MAX_SPREAD_PIPS", "invalid")
    monkeypatch.setenv("ML_PREDICT_THRESHOLD", "invalid")
    assert read_fixed_risk_usd() == 0.50
    assert read_max_spread_pips() == 3.5
    assert read_predict_threshold() == -1.0


def test_policy_resolution_defaults_to_core_srs(monkeypatch):
    _clear_policy_env(monkeypatch)
    from config_microcapital import get_policy_config

    policy = get_policy_config()
    assert policy["MODE_ID"] == "core_srs"
    assert policy["MODE_LABEL"] == "Core SRS"
    assert policy["EVIDENCE_LABEL"] == "Core SRS v1"
    assert policy["FIXED_RISK_USD"] is None
    assert policy["DAILY_STOP_LOSS_PCT"] == 0.08
    assert policy["WEEKLY_STOP_LOSS_PCT"] == 0.15
    assert policy["HARD_DRAWDOWN_PCT"] == 0.20
    assert policy["MAX_SIMULTANEOUS_TRADES"] == 2
    assert policy["LOSS_HALT_THRESHOLD"] == 3


def test_preserve_10_is_explicit_mode_not_legacy_alias(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    from config_microcapital import (
        LEGACY_MICRO_CAPITAL_CONFIG,
        PRESERVE_10_CONFIG,
        get_policy_config,
    )

    policy = get_policy_config()
    assert PRESERVE_10_CONFIG is not LEGACY_MICRO_CAPITAL_CONFIG
    assert policy["MODE_ID"] == "preserve_10"
    assert policy["MODE_LABEL"] == "Preserve-$10"
    assert policy["EVIDENCE_LABEL"] == "Preserve-$10 doctrine"
    assert policy["FIXED_RISK_USD"] == 0.50
    assert policy["MAX_SIMULTANEOUS_TRADES"] == 1
    assert policy["DAILY_STOP_LOSS_PCT"] == 0.15
    assert policy["WEEKLY_STOP_LOSS_PCT"] == 0.25
    assert policy["HARD_DRAWDOWN_PCT"] == 0.30
    assert policy["LOSS_HALT_THRESHOLD"] == 2


def test_explicit_policy_mode_overrides_legacy_micro_capital_toggle(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    monkeypatch.setenv("MICRO_CAPITAL_MODE", "1")
    from config_microcapital import get_policy_config

    policy = get_policy_config()
    assert policy["MODE_ID"] == "preserve_10"


def test_hard_risk_engine_reads_legacy_micro_capital_mode(monkeypatch):
    """HardRiskEngine must use relaxed legacy limits when MICRO_CAPITAL_MODE=1."""
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("MICRO_CAPITAL_MODE", "1")
    from core.risk.hard_risk_engine import HardRiskEngine

    engine = HardRiskEngine()
    assert engine.mode_id == "legacy_micro_capital"
    assert engine.max_daily_loss == 0.15
    assert engine.max_weekly_loss == 0.25
    assert engine.max_simultaneous_trades == 1


def test_hard_risk_engine_reads_explicit_preserve_10_mode(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    from core.risk.hard_risk_engine import HardRiskEngine, _read_fixed_risk_usd

    engine = HardRiskEngine()
    assert engine.mode_id == "preserve_10"
    assert engine.evidence_label == "Preserve-$10 doctrine"
    assert _read_fixed_risk_usd() == 0.50


def test_core_srs_ignores_governed_runtime_env_overrides(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FIXED_RISK_USD", "0.75")
    monkeypatch.setenv("MAX_SPREAD_PIPS", "9.0")
    monkeypatch.setenv("ML_PREDICT_THRESHOLD", "-1.0")
    import pandas as pd
    from config_microcapital import (
        read_fixed_risk_usd,
        read_max_spread_pips,
        read_predict_threshold,
    )
    from core.agents.adversarial_agent import AdversarialAgent
    from core.agents.portfolio_manager import PortfolioManager

    manager = PortfolioManager()
    agent = AdversarialAgent(
        symbol="EURUSD",
        fetch_ohlc=lambda s, t, n: pd.DataFrame(),
        fetch_spread=lambda s: None,
    )

    assert read_fixed_risk_usd() is None
    assert read_max_spread_pips() == 2.0
    assert read_predict_threshold() == 0.0
    assert manager.mode_id == "core_srs"
    assert manager.fixed_risk_usd is None
    assert agent.mode_id == "core_srs"
    assert agent.max_spread_pips == 2.0


def test_preserve_10_allows_governed_runtime_env_overrides(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    monkeypatch.setenv("FIXED_RISK_USD", "0.75")
    monkeypatch.setenv("MAX_SPREAD_PIPS", "4.2")
    monkeypatch.setenv("ML_PREDICT_THRESHOLD", "-0.25")
    import pandas as pd
    from config_microcapital import (
        read_fixed_risk_usd,
        read_max_spread_pips,
        read_predict_threshold,
    )
    from core.agents.adversarial_agent import AdversarialAgent
    from core.agents.portfolio_manager import PortfolioManager

    manager = PortfolioManager()
    agent = AdversarialAgent(
        symbol="EURUSD",
        fetch_ohlc=lambda s, t, n: pd.DataFrame(),
        fetch_spread=lambda s: None,
    )

    assert read_fixed_risk_usd() == 0.75
    assert read_max_spread_pips() == 4.2
    assert read_predict_threshold() == -0.25
    assert manager.mode_id == "preserve_10"
    assert manager.fixed_risk_usd == 0.75
    assert agent.mode_id == "preserve_10"
    assert agent.max_spread_pips == 4.2


def test_adversarial_agent_reads_max_spread_pips(monkeypatch):
    """AdversarialAgent must read preserve-$10 mode defaults at instantiation."""
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    import pandas as pd
    from core.agents.adversarial_agent import AdversarialAgent

    agent = AdversarialAgent(
        symbol="EURUSD",
        fetch_ohlc=lambda s, t, n: pd.DataFrame(),
        fetch_spread=lambda s: None,
    )
    assert agent.mode_id == "preserve_10"
    assert agent.max_spread_pips == 3.5


def test_portfolio_manager_reads_preserve_10_fixed_risk(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    from core.agents.portfolio_manager import PortfolioManager

    manager = PortfolioManager()
    assert manager.mode_id == "preserve_10"
    assert manager.fixed_risk_usd == 0.50
