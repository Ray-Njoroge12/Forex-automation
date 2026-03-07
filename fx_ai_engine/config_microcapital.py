"""Explicit runtime policy profiles for Core SRS and preserve-$10 trading.

Usage:
    Core SRS baseline (default):
        python main.py --mode demo

    Explicit Preserve-$10 mode:
        set FX_POLICY_MODE=preserve_10
        python main.py --mode demo

    Legacy micro-capital fallback (backward compatible):
        set MICRO_CAPITAL_MODE=1
        python main.py --mode demo
"""

from __future__ import annotations

import logging
import os
from typing import Mapping

logger = logging.getLogger(__name__)

POLICY_MODE_ENV = "FX_POLICY_MODE"
LEGACY_MICRO_CAPITAL_ENV = "MICRO_CAPITAL_MODE"

_SHARED_POLICY = {
    "ENGINE_POLICY_FAMILY": "shared_fx_engine",
    "BASE_RISK_PCT": 0.032,
    "MAX_COMBINED_EXPOSURE": 0.05,
    "MIN_RISK_REWARD": 2.2,
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
        return LEGACY_MICRO_CAPITAL_CONFIG["MODE_ID"]
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
    return MODE_CONFIGS[resolved_mode].copy()


def _mode_allows_runtime_overrides(env: Mapping[str, str] | None = None) -> bool:
    """Return whether governed runtime env overrides are enabled."""
    source = os.environ if env is None else env
    return resolve_policy_mode(source) in OVERRIDE_ENABLED_MODE_IDS


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
