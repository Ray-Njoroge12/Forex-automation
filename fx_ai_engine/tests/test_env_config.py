from __future__ import annotations

import pytest


def _clear_policy_env(monkeypatch) -> None:
    for name in (
        "FX_POLICY_MODE",
        "FX_ALLOW_NON_SRS_POLICY",
        "MICRO_CAPITAL_MODE",
        "FIXED_RISK_USD",
        "MAX_SPREAD_PIPS",
        "ML_PREDICT_THRESHOLD",
        "FX_EXPERIMENT_PAIR_SELECTIVE_RISING_ADX_RELAX",
        "FX_EXPERIMENT_AUDUSD_PULLBACK_RELAX",
        "FX_EXPERIMENT_LIVE_TRADE_MGMT_OPTION_C",
    ):
        monkeypatch.delenv(name, raising=False)


def test_governed_runtime_overrides_require_explicit_non_srs_approval(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    monkeypatch.setenv("FIXED_RISK_USD", "0.75")
    monkeypatch.setenv("MAX_SPREAD_PIPS", "4.2")
    monkeypatch.setenv("ML_PREDICT_THRESHOLD", "-0.25")
    from config_microcapital import (
        read_fixed_risk_usd,
        read_max_spread_pips,
        read_predict_threshold,
    )

    assert read_fixed_risk_usd() == 0.50
    assert read_max_spread_pips() == 3.5
    assert read_predict_threshold() == -1.0

    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
    assert read_fixed_risk_usd() == 0.75
    assert read_max_spread_pips() == 4.2
    assert read_predict_threshold() == -0.25

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


def test_policy_resolution_ignores_deprecated_micro_capital_toggle(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("MICRO_CAPITAL_MODE", "1")
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


def test_policy_thresholds_are_deep_copied_and_mergeable(monkeypatch):
    _clear_policy_env(monkeypatch)
    from config_microcapital import apply_agent_threshold_overrides, get_policy_config

    policy = get_policy_config()
    policy["AGENT_THRESHOLDS"]["REGIME"]["adx_no_trade_below"] = 18.0

    fresh = get_policy_config()
    assert fresh["AGENT_THRESHOLDS"]["REGIME"]["adx_no_trade_below"] == 20.0

    merged = apply_agent_threshold_overrides(
        fresh,
        {
            "AGENT_THRESHOLDS": {
                "REGIME": {"adx_no_trade_below": 18.0},
                "TECHNICAL": {"buy_rsi_min": 39.0},
            }
        },
    )

    assert merged["AGENT_THRESHOLDS"]["REGIME"]["adx_no_trade_below"] == 18.0
    assert merged["AGENT_THRESHOLDS"]["TECHNICAL"]["buy_rsi_min"] == 39.0


def test_runtime_experiment_defaults_to_disabled(monkeypatch):
    _clear_policy_env(monkeypatch)
    from config_microcapital import apply_runtime_experiment_config, get_policy_config

    policy = apply_runtime_experiment_config(get_policy_config(), run_mode="demo")

    assert policy["EXPERIMENTS"]["PAIR_SELECTIVE_RISING_ADX_RELAX"]["enabled"] is False
    assert policy["EXPERIMENTS"]["AUDUSD_PULLBACK_RELAX"]["enabled"] is False
    assert policy["EXPERIMENTS"]["LIVE_TRADE_MGMT_OPTION_C"]["enabled"] is False
    assert "EXPERIMENT_TAG" not in policy


def test_runtime_experiment_enables_only_for_core_srs_demo(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_EXPERIMENT_PAIR_SELECTIVE_RISING_ADX_RELAX", "1")
    from config_microcapital import (
        PAIR_SELECTIVE_RISING_ADX_RELAX_TAG,
        apply_runtime_experiment_config,
        get_policy_config,
    )

    demo_policy = apply_runtime_experiment_config(get_policy_config(), run_mode="demo")
    smoke_policy = apply_runtime_experiment_config(get_policy_config(), run_mode="smoke")

    assert demo_policy["EXPERIMENTS"]["PAIR_SELECTIVE_RISING_ADX_RELAX"]["enabled"] is True
    assert demo_policy["EXPERIMENT_TAG"] == PAIR_SELECTIVE_RISING_ADX_RELAX_TAG
    assert smoke_policy["EXPERIMENTS"]["PAIR_SELECTIVE_RISING_ADX_RELAX"]["enabled"] is False
    assert "EXPERIMENT_TAG" not in smoke_policy


def test_runtime_audusd_pullback_experiment_enables_only_for_core_srs_demo(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_EXPERIMENT_AUDUSD_PULLBACK_RELAX", "1")
    from config_microcapital import (
        AUDUSD_PULLBACK_RELAX_TAG,
        apply_runtime_experiment_config,
        get_policy_config,
    )

    demo_policy = apply_runtime_experiment_config(get_policy_config(), run_mode="demo")
    smoke_policy = apply_runtime_experiment_config(get_policy_config(), run_mode="smoke")

    assert demo_policy["EXPERIMENTS"]["AUDUSD_PULLBACK_RELAX"]["enabled"] is True
    assert demo_policy["EXPERIMENT_TAG"] == AUDUSD_PULLBACK_RELAX_TAG
    assert smoke_policy["EXPERIMENTS"]["AUDUSD_PULLBACK_RELAX"]["enabled"] is False
    assert "EXPERIMENT_TAG" not in smoke_policy


def test_runtime_live_trade_mgmt_option_c_enables_only_for_core_srs_demo(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_EXPERIMENT_LIVE_TRADE_MGMT_OPTION_C", "1")
    from config_microcapital import (
        LIVE_TRADE_MGMT_OPTION_C_TAG,
        apply_runtime_experiment_config,
        get_policy_config,
    )

    demo_policy = apply_runtime_experiment_config(get_policy_config(), run_mode="demo")
    smoke_policy = apply_runtime_experiment_config(get_policy_config(), run_mode="smoke")

    assert demo_policy["EXPERIMENTS"]["LIVE_TRADE_MGMT_OPTION_C"]["enabled"] is True
    assert demo_policy["EXPERIMENT_TAG"] == LIVE_TRADE_MGMT_OPTION_C_TAG
    assert smoke_policy["EXPERIMENTS"]["LIVE_TRADE_MGMT_OPTION_C"]["enabled"] is False
    assert "EXPERIMENT_TAG" not in smoke_policy


def test_runtime_evidence_context_appends_experiment_stream_suffix(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_EXPERIMENT_PAIR_SELECTIVE_RISING_ADX_RELAX", "1")
    from config_microcapital import apply_runtime_experiment_config, get_policy_config
    from core.evidence import build_runtime_evidence_context

    policy = apply_runtime_experiment_config(get_policy_config(), run_mode="demo")
    ctx = build_runtime_evidence_context(policy, use_mock=False, login=123, server="Demo-Server")

    assert ctx.evidence_stream == "runtime_mt5_core_srs__pair_selective_rising_adx_relax"
    assert ctx.policy_mode == "core_srs"
    assert ctx.account_scope == "mt5:Demo-Server:123"


def test_runtime_evidence_context_appends_audusd_pullback_experiment_stream_suffix(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_EXPERIMENT_AUDUSD_PULLBACK_RELAX", "1")
    from config_microcapital import apply_runtime_experiment_config, get_policy_config
    from core.evidence import build_runtime_evidence_context

    policy = apply_runtime_experiment_config(get_policy_config(), run_mode="demo")
    ctx = build_runtime_evidence_context(policy, use_mock=False, login=123, server="Demo-Server")

    assert ctx.evidence_stream == "runtime_mt5_core_srs__audusd_pullback_relax"
    assert ctx.policy_mode == "core_srs"
    assert ctx.account_scope == "mt5:Demo-Server:123"


def test_runtime_evidence_context_appends_live_trade_mgmt_option_c_stream_suffix(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_EXPERIMENT_LIVE_TRADE_MGMT_OPTION_C", "1")
    from config_microcapital import apply_runtime_experiment_config, get_policy_config
    from core.evidence import build_runtime_evidence_context

    policy = apply_runtime_experiment_config(get_policy_config(), run_mode="demo")
    ctx = build_runtime_evidence_context(policy, use_mock=False, login=123, server="Demo-Server")

    assert ctx.evidence_stream == "runtime_mt5_core_srs__live_trade_mgmt_option_c"
    assert ctx.policy_mode == "core_srs"
    assert ctx.account_scope == "mt5:Demo-Server:123"


def test_agent_threshold_overrides_reject_locked_fields(monkeypatch):
    _clear_policy_env(monkeypatch)
    from config_microcapital import apply_agent_threshold_overrides, get_policy_config

    with pytest.raises(ValueError, match="Unsupported TECHNICAL threshold override"):
        apply_agent_threshold_overrides(
            get_policy_config(),
            {"AGENT_THRESHOLDS": {"TECHNICAL": {"min_rr": 2.0}}},
        )


def test_preserve_10_is_explicit_mode_not_legacy_alias(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
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
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    monkeypatch.setenv("MICRO_CAPITAL_MODE", "1")
    from config_microcapital import get_policy_config

    policy = get_policy_config()
    assert policy["MODE_ID"] == "preserve_10"


def test_hard_risk_engine_ignores_deprecated_legacy_micro_capital_toggle(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("MICRO_CAPITAL_MODE", "1")
    from core.risk.hard_risk_engine import HardRiskEngine

    engine = HardRiskEngine()
    assert engine.mode_id == "core_srs"
    assert engine.max_daily_loss == 0.08
    assert engine.max_weekly_loss == 0.15
    assert engine.max_simultaneous_trades == 2


def test_hard_risk_engine_reads_explicit_preserve_10_mode(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
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
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
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
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
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
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    from core.agents.portfolio_manager import PortfolioManager

    manager = PortfolioManager()
    assert manager.mode_id == "preserve_10"
    assert manager.fixed_risk_usd == 0.50


def test_mock_mode_uses_isolated_bridge_path_by_default(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.delenv("BRIDGE_BASE_PATH", raising=False)

    from core.bridge_utils import get_mt5_bridge_path

    bridge_path = get_mt5_bridge_path()

    assert bridge_path.name == "mock_mt5_bridge"
    assert bridge_path.as_posix().endswith("/fx_ai_engine/mock_mt5_bridge")


def test_mock_mode_ignores_bridge_base_path_and_uses_dedicated_override(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setenv("BRIDGE_BASE_PATH", "/tmp/live_bridge_should_be_ignored")
    monkeypatch.setenv("MT5_MOCK_BRIDGE_PATH", "/tmp/mock_bridge_override")

    from core.bridge_utils import get_mt5_bridge_path

    bridge_path = get_mt5_bridge_path()

    assert bridge_path.as_posix() == "/tmp/mock_bridge_override"


def test_bridge_base_path_windows_env_is_coerced_under_wsl(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.delenv("USE_MT5_MOCK", raising=False)
    monkeypatch.setenv("BRIDGE_BASE_PATH", r"C:\Users\rayng\AppData\Roaming\MetaQuotes\Terminal\ABC\MQL5\Files\bridge")

    import core.bridge_utils as bridge_utils

    monkeypatch.setattr(bridge_utils, "_is_windows_runtime", lambda: False)

    bridge_path = bridge_utils.get_mt5_bridge_path()

    assert bridge_path.as_posix() == "/mnt/c/Users/rayng/AppData/Roaming/MetaQuotes/Terminal/ABC/MQL5/Files/bridge"


def test_mock_bridge_override_windows_env_is_coerced_under_wsl(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setenv("MT5_MOCK_BRIDGE_PATH", r"D:\temp\mock_bridge")

    import core.bridge_utils as bridge_utils

    monkeypatch.setattr(bridge_utils, "_is_windows_runtime", lambda: False)

    bridge_path = bridge_utils.get_mt5_bridge_path()

    assert bridge_path.as_posix() == "/mnt/d/temp/mock_bridge"


def test_bridge_base_path_windows_env_not_coerced_on_windows(monkeypatch):
    _clear_policy_env(monkeypatch)
    monkeypatch.delenv("USE_MT5_MOCK", raising=False)
    monkeypatch.setenv("BRIDGE_BASE_PATH", r"C:\Users\rayng\AppData\Roaming\MetaQuotes\Terminal\ABC\MQL5\Files\bridge")

    import core.bridge_utils as bridge_utils

    monkeypatch.setattr(bridge_utils, "_is_windows_runtime", lambda: True)

    bridge_path = bridge_utils.get_mt5_bridge_path()

    assert str(bridge_path) == r"C:\Users\rayng\AppData\Roaming\MetaQuotes\Terminal\ABC\MQL5\Files\bridge"


def test_mock_runtime_state_path_is_namespaced_by_policy(monkeypatch, tmp_path):
    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")

    from core.bridge_utils import get_mock_runtime_state_path

    state_path = get_mock_runtime_state_path(tmp_path)

    assert state_path == tmp_path / "mock_runtime_state.preserve_10.json"
