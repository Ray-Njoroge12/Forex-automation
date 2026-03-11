"""Explicit runtime policy profiles for Core SRS and research-only non-SRS trading.

Usage:
    Core SRS baseline (default):
        python main.py --mode demo

    Explicit Preserve-$10 research mode:
        set FX_ALLOW_NON_SRS_POLICY=1
        set FX_POLICY_MODE=preserve_10
        python main.py --mode demo

    Explicit legacy micro-capital research mode:
        set FX_ALLOW_NON_SRS_POLICY=1
        set FX_POLICY_MODE=legacy_micro_capital
        python main.py --mode demo
"""

from __future__ import annotations

from copy import deepcopy
import logging
import os
from typing import Mapping

logger = logging.getLogger(__name__)

POLICY_MODE_ENV = "FX_POLICY_MODE"
LEGACY_MICRO_CAPITAL_ENV = "MICRO_CAPITAL_MODE"
ALLOW_NON_SRS_POLICY_ENV = "FX_ALLOW_NON_SRS_POLICY"
PAIR_SELECTIVE_RISING_ADX_RELAX_ENV = "FX_EXPERIMENT_PAIR_SELECTIVE_RISING_ADX_RELAX"
PAIR_SELECTIVE_RISING_ADX_RELAX_TAG = "pair_selective_rising_adx_relax"
AUDUSD_PULLBACK_RELAX_ENV = "FX_EXPERIMENT_AUDUSD_PULLBACK_RELAX"
AUDUSD_PULLBACK_RELAX_TAG = "audusd_pullback_relax"
LIVE_TRADE_MGMT_OPTION_C_ENV = "FX_EXPERIMENT_LIVE_TRADE_MGMT_OPTION_C"
LIVE_TRADE_MGMT_OPTION_C_TAG = "live_trade_mgmt_option_c"

_PAIR_SELECTIVE_RISING_ADX_RELAX = {
    "enabled": False,
    "symbols": ["EURUSD", "USDJPY"],
    "adx_lookback_bars": 5,
    "adx_rise_min": 1.0,
    "adx_no_trade_below": 18.0,
    "adx_transition_below": 22.0,
}

_AUDUSD_PULLBACK_RELAX = {
    "enabled": False,
    "symbols": ["AUDUSD"],
    "pullback_buffer_pips": 4.0,
}

_LIVE_TRADE_MGMT_OPTION_C = {
    "enabled": False,
    "low_normal": {
        "be_trigger_r": 0.5,
        "partial_close_r": 1.0,
        "trailing_atr_mult": 1.5,
        "tp_mode": "HYBRID",
    },
    "high": {
        "be_trigger_r": 0.75,
        "partial_close_r": 1.25,
        "trailing_atr_mult": 2.0,
        "tp_mode": "HYBRID",
    },
}

_SHARED_POLICY = {
    "ENGINE_POLICY_FAMILY": "shared_fx_engine",
    "BASE_RISK_PCT": 0.032,
    "MAX_COMBINED_EXPOSURE": 0.05,
    "MIN_RISK_REWARD": 2.2,
    "AGENT_THRESHOLDS": {
        "REGIME": {
            "ema_fast": 50,
            "ema_slow": 200,
            "atr_period": 14,
            "adx_period": 14,
            "atr_lookback": 20,
            "trend_distance_threshold": 0.0005,
            "adx_no_trade_below": 20.0,
            "adx_transition_below": 25.0,
            "high_vol_atr_ratio": 1.25,
            "low_vol_atr_ratio": 0.75,
            "confidence_adx_cap": 30.0,
        },
        "TECHNICAL": {
            "ema_fast": 50,
            "ema_slow": 200,
            "atr_period": 14,
            "rsi_period": 14,
            "stop_atr_multiplier": 1.2,
            "pullback_buffer_pips": 2.0,
            "buy_rsi_min": 40.0,
            "buy_rsi_max": 65.0,
            "sell_rsi_min": 35.0,
            "sell_rsi_max": 60.0,
            "structural_lookback": 20,
            "structural_ratio_min": 0.8,
            "structural_ratio_max": 1.5,
        },
    },
    "EXPERIMENTS": {
        "PAIR_SELECTIVE_RISING_ADX_RELAX": _PAIR_SELECTIVE_RISING_ADX_RELAX,
        "AUDUSD_PULLBACK_RELAX": _AUDUSD_PULLBACK_RELAX,
        "LIVE_TRADE_MGMT_OPTION_C": _LIVE_TRADE_MGMT_OPTION_C,
    },
}

CORE_SRS_CONFIG = {
    **_SHARED_POLICY,
    "MODE_ID": "core_srs",
    "MODE_LABEL": "Core SRS",
    "EVIDENCE_LABEL": "Core SRS v1",
    "FIXED_RISK_USD": None,
    "MAX_SIMULTANEOUS_TRADES": 2,
    "DAILY_STOP_LOSS_PCT": 0.08,
    "WEEKLY_STOP_LOSS_PCT": 0.15,
    "HARD_DRAWDOWN_PCT": 0.20,
    "CONSECUTIVE_LOSS_HALT": 3,
    "LOSS_HALT_THRESHOLD": 3,
    "LOSS_THROTTLE_STEPS": {1: 0.75, 2: 0.50},
    "MAX_SPREAD_PIPS": 2.0,
    "ML_PREDICT_THRESHOLD": 0.0,
}

LEGACY_MICRO_CAPITAL_CONFIG = {
    **_SHARED_POLICY,
    "MODE_ID": "legacy_micro_capital",
    "MODE_LABEL": "Legacy Micro-Capital",
    "EVIDENCE_LABEL": "Legacy micro-capital path",
    "FIXED_RISK_USD": 0.50,
    "MAX_SIMULTANEOUS_TRADES": 1,
    "DAILY_STOP_LOSS_PCT": 0.15,
    "WEEKLY_STOP_LOSS_PCT": 0.25,
    "HARD_DRAWDOWN_PCT": 0.30,
    "CONSECUTIVE_LOSS_HALT": 2,
    "LOSS_HALT_THRESHOLD": 2,
    "LOSS_THROTTLE_STEPS": {1: 0.75},
    "MAX_SPREAD_PIPS": 3.5,
    "ML_PREDICT_THRESHOLD": -1.0,
}

PRESERVE_10_CONFIG = {
    **LEGACY_MICRO_CAPITAL_CONFIG,
    "MODE_ID": "preserve_10",
    "MODE_LABEL": "Preserve-$10",
    "EVIDENCE_LABEL": "Preserve-$10 doctrine",
}

MODE_CONFIGS = {
    CORE_SRS_CONFIG["MODE_ID"]: CORE_SRS_CONFIG,
    LEGACY_MICRO_CAPITAL_CONFIG["MODE_ID"]: LEGACY_MICRO_CAPITAL_CONFIG,
    PRESERVE_10_CONFIG["MODE_ID"]: PRESERVE_10_CONFIG,
}

NON_SRS_MODE_IDS = {
    LEGACY_MICRO_CAPITAL_CONFIG["MODE_ID"],
    PRESERVE_10_CONFIG["MODE_ID"],
}

OVERRIDE_ENABLED_MODE_IDS = {
    LEGACY_MICRO_CAPITAL_CONFIG["MODE_ID"],
    PRESERVE_10_CONFIG["MODE_ID"],
}

MICRO_CAPITAL_CONFIG = LEGACY_MICRO_CAPITAL_CONFIG
STANDARD_CAPITAL_CONFIG = CORE_SRS_CONFIG

COMPOUNDING_MILESTONES = {
    10: {"risk_usd": 0.50, "max_trades": 1},
    20: {"risk_usd": 0.75, "max_trades": 1},
    50: {"risk_usd": 1.50, "max_trades": 1},
    100: {"risk_usd": 3.00, "max_trades": 2},
    200: {"risk_usd": 6.00, "max_trades": 2},
    500: {"risk_usd": None, "max_trades": 2},
}


def resolve_policy_mode(env: Mapping[str, str] | None = None) -> str:
    """Resolve the active runtime policy mode."""
    source = os.environ if env is None else env
    explicit = source.get(POLICY_MODE_ENV, "").strip().lower()
    if explicit:
        if explicit not in MODE_CONFIGS:
            raise ValueError(
                f"Unsupported {POLICY_MODE_ENV}={explicit!r}. "
                f"Expected one of: {', '.join(sorted(MODE_CONFIGS))}"
            )
        return explicit
    if source.get(LEGACY_MICRO_CAPITAL_ENV, "").strip() == "1":
        logger.warning(
            "Ignoring deprecated %s=1; set %s explicitly and %s=1 for research-only non-SRS runtime modes",
            LEGACY_MICRO_CAPITAL_ENV,
            POLICY_MODE_ENV,
            ALLOW_NON_SRS_POLICY_ENV,
        )
    return CORE_SRS_CONFIG["MODE_ID"]


def get_policy_config(
    mode_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Return a copy of the requested or active runtime policy config."""
    resolved_mode = resolve_policy_mode(env) if mode_id is None else mode_id.strip().lower()
    if resolved_mode not in MODE_CONFIGS:
        raise ValueError(
            f"Unknown policy mode {resolved_mode!r}. "
            f"Expected one of: {', '.join(sorted(MODE_CONFIGS))}"
        )
    return deepcopy(MODE_CONFIGS[resolved_mode])


def _read_bool_env(name: str, env: Mapping[str, str] | None = None) -> bool | None:
    source = os.environ if env is None else env
    raw = source.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    logger.warning("Invalid %s=%r, expected one of 1/0 true/false yes/no on/off", name, raw)
    return None


def is_non_srs_policy_mode(mode_id: str | None) -> bool:
    return str(mode_id or "").strip().lower() in NON_SRS_MODE_IDS


def non_srs_policy_allowed(env: Mapping[str, str] | None = None) -> bool:
    return _read_bool_env(ALLOW_NON_SRS_POLICY_ENV, env) is True


def apply_runtime_experiment_config(
    policy: Mapping[str, object],
    *,
    run_mode: str,
    env: Mapping[str, str] | None = None,
) -> dict:
    merged = deepcopy(dict(policy))
    experiments = merged.setdefault("EXPERIMENTS", {})
    if not isinstance(experiments, dict):
        experiments = {}
        merged["EXPERIMENTS"] = experiments

    mode_id = str(merged.get("MODE_ID", "") or "").strip().lower()
    run_mode_norm = str(run_mode or "").strip().lower()
    allow_runtime_enable = mode_id == CORE_SRS_CONFIG["MODE_ID"] and run_mode_norm == "demo"

    runtime_experiments = (
        (
            "PAIR_SELECTIVE_RISING_ADX_RELAX",
            _PAIR_SELECTIVE_RISING_ADX_RELAX,
            PAIR_SELECTIVE_RISING_ADX_RELAX_ENV,
            PAIR_SELECTIVE_RISING_ADX_RELAX_TAG,
        ),
        (
            "AUDUSD_PULLBACK_RELAX",
            _AUDUSD_PULLBACK_RELAX,
            AUDUSD_PULLBACK_RELAX_ENV,
            AUDUSD_PULLBACK_RELAX_TAG,
        ),
        (
            "LIVE_TRADE_MGMT_OPTION_C",
            _LIVE_TRADE_MGMT_OPTION_C,
            LIVE_TRADE_MGMT_OPTION_C_ENV,
            LIVE_TRADE_MGMT_OPTION_C_TAG,
        ),
    )
    active_tags: list[str] = []
    for key, defaults, env_name, tag in runtime_experiments:
        experiment = deepcopy(defaults)
        existing = experiments.get(key, {})
        if isinstance(existing, Mapping):
            experiment.update(existing)

        enabled = bool(experiment.get("enabled", False))
        requested = _read_bool_env(env_name, env)
        if requested is not None:
            enabled = requested
        if enabled and not allow_runtime_enable:
            logger.warning(
                "Ignoring %s for mode=%s run_mode=%s; experiment is demo-only for core_srs",
                env_name,
                mode_id or "unknown",
                run_mode_norm or "unknown",
            )
            enabled = False

        experiment["enabled"] = enabled
        experiments[key] = experiment
        if enabled:
            active_tags.append(tag)

    if active_tags:
        merged["EXPERIMENT_TAG"] = "__".join(active_tags)
    else:
        merged.pop("EXPERIMENT_TAG", None)
    return merged


_ALLOWED_AGENT_THRESHOLD_OVERRIDES = {
    "REGIME": {
        "ema_fast",
        "ema_slow",
        "atr_period",
        "adx_period",
        "atr_lookback",
        "trend_distance_threshold",
        "adx_no_trade_below",
        "adx_transition_below",
        "high_vol_atr_ratio",
        "low_vol_atr_ratio",
        "confidence_adx_cap",
    },
    "TECHNICAL": {
        "ema_fast",
        "ema_slow",
        "atr_period",
        "rsi_period",
        "stop_atr_multiplier",
        "pullback_buffer_pips",
        "buy_rsi_min",
        "buy_rsi_max",
        "sell_rsi_min",
        "sell_rsi_max",
        "structural_lookback",
        "structural_ratio_min",
        "structural_ratio_max",
    },
}


def apply_agent_threshold_overrides(
    policy: Mapping[str, object], overrides: Mapping[str, object] | None = None
) -> dict:
    """Return a policy copy with validated, research-only agent-threshold overrides applied."""
    merged = deepcopy(dict(policy))
    if not overrides:
        return merged

    if set(overrides) != {"AGENT_THRESHOLDS"}:
        raise ValueError("Only AGENT_THRESHOLDS overrides are supported")

    threshold_groups = overrides.get("AGENT_THRESHOLDS", {})
    if not isinstance(threshold_groups, Mapping):
        raise ValueError("AGENT_THRESHOLDS overrides must be a mapping")

    target_thresholds = merged.setdefault("AGENT_THRESHOLDS", {})
    if not isinstance(target_thresholds, dict):
        raise ValueError("Policy AGENT_THRESHOLDS must be a dict")

    for group_name, group_overrides in threshold_groups.items():
        if group_name not in _ALLOWED_AGENT_THRESHOLD_OVERRIDES:
            raise ValueError(f"Unsupported agent threshold group: {group_name}")
        if not isinstance(group_overrides, Mapping):
            raise ValueError(f"Overrides for {group_name} must be a mapping")
        invalid = set(group_overrides) - _ALLOWED_AGENT_THRESHOLD_OVERRIDES[group_name]
        if invalid:
            invalid_names = ", ".join(sorted(invalid))
            raise ValueError(f"Unsupported {group_name} threshold override(s): {invalid_names}")
        target_group = target_thresholds.setdefault(group_name, {})
        if not isinstance(target_group, dict):
            raise ValueError(f"Policy AGENT_THRESHOLDS[{group_name}] must be a dict")
        for key, value in group_overrides.items():
            target_group[key] = deepcopy(value)

    return merged


def _mode_allows_runtime_overrides(env: Mapping[str, str] | None = None) -> bool:
    """Return whether governed runtime env overrides are enabled."""
    source = os.environ if env is None else env
    return resolve_policy_mode(source) in OVERRIDE_ENABLED_MODE_IDS and non_srs_policy_allowed(source)


def _read_governed_float_override(
    env_name: str,
    policy_key: str,
    env: Mapping[str, str] | None = None,
    *,
    min_value: float | None = None,
) -> float | None:
    """Return a mode-governed float override or the labeled policy default."""
    source = os.environ if env is None else env
    policy = get_policy_config(env=source)
    default = policy[policy_key]
    raw = source.get(env_name, "").strip()
    if not raw or not _mode_allows_runtime_overrides(source):
        return default

    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r for mode=%s, defaulting to labeled policy value %r",
            env_name,
            raw,
            policy["MODE_ID"],
            default,
        )
        return default

    if min_value is not None and value < min_value:
        logger.warning(
            "%s=%r is below minimum %.3f for mode=%s, defaulting to labeled policy value %r",
            env_name,
            raw,
            min_value,
            policy["MODE_ID"],
            default,
        )
        return default

    return value


def read_fixed_risk_usd(env: Mapping[str, str] | None = None) -> float | None:
    """Return fixed-risk USD, honoring env only for explicit non-Core modes."""
    return _read_governed_float_override(
        "FIXED_RISK_USD",
        "FIXED_RISK_USD",
        env,
        min_value=0.0000001,
    )


def read_max_spread_pips(env: Mapping[str, str] | None = None) -> float:
    """Return spread ceiling, honoring env only for explicit non-Core modes."""
    value = _read_governed_float_override(
        "MAX_SPREAD_PIPS",
        "MAX_SPREAD_PIPS",
        env,
        min_value=0.0000001,
    )
    return float(value)


def read_predict_threshold(env: Mapping[str, str] | None = None) -> float:
    """Return ML threshold, honoring env only for explicit non-Core modes."""
    value = _read_governed_float_override(
        "ML_PREDICT_THRESHOLD",
        "ML_PREDICT_THRESHOLD",
        env,
    )
    return float(value)


def default_predict_threshold(env: Mapping[str, str] | None = None) -> float:
    """Return the mode-default ML threshold."""
    source = os.environ if env is None else env
    return float(get_policy_config(env=source)["ML_PREDICT_THRESHOLD"])


def get_config_for_balance(balance: float) -> dict:
    """Return legacy micro-capital compounding config for a balance."""
    if balance < 500:
        for threshold in sorted(COMPOUNDING_MILESTONES.keys(), reverse=True):
            if balance >= threshold:
                milestone = COMPOUNDING_MILESTONES[threshold]
                config = LEGACY_MICRO_CAPITAL_CONFIG.copy()
                config["FIXED_RISK_USD"] = milestone["risk_usd"]
                config["MAX_SIMULTANEOUS_TRADES"] = milestone["max_trades"]
                return config
        return LEGACY_MICRO_CAPITAL_CONFIG.copy()
    return CORE_SRS_CONFIG.copy()


def print_config_summary(balance: float):
    """Print a summary of the active configuration for given balance."""
    config = get_config_for_balance(balance)
    print("=" * 60)
    print(f"RISK CONFIGURATION FOR ${balance:.2f} ACCOUNT")
    print("=" * 60)
    print(f"Mode: {config['MODE_LABEL']} ({config['EVIDENCE_LABEL']})")
    if config["FIXED_RISK_USD"]:
        risk_pct = (config["FIXED_RISK_USD"] / balance) * 100
        print(f"Risk per trade: ${config['FIXED_RISK_USD']:.2f} ({risk_pct:.1f}%)")
    else:
        print("Risk per trade: 3.2% (percentage mode)")
    print(f"Max simultaneous trades: {config['MAX_SIMULTANEOUS_TRADES']}")
    print(f"Daily stop loss: {config['DAILY_STOP_LOSS_PCT']:.1%} (${balance * config['DAILY_STOP_LOSS_PCT']:.2f})")
    print(f"Weekly stop loss: {config['WEEKLY_STOP_LOSS_PCT']:.1%} (${balance * config['WEEKLY_STOP_LOSS_PCT']:.2f})")
    print(f"Hard drawdown: {config['HARD_DRAWDOWN_PCT']:.1%} (${balance * config['HARD_DRAWDOWN_PCT']:.2f})")
    print(f"Consecutive loss halt: {config['CONSECUTIVE_LOSS_HALT']}")
    print(f"Max spread: {config['MAX_SPREAD_PIPS']} pips")
    print(f"ML threshold: {config['ML_PREDICT_THRESHOLD']}")
    print("=" * 60)


if __name__ == "__main__":
    for balance in [10, 25, 50, 100, 250, 500, 1000]:
        print_config_summary(balance)
        print()
