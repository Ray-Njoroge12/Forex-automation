"""Guard the SRS v1 locked constants (AGENTS.md, SRS_v1.md section 6).

A failure here means a locked risk limit changed. That requires an explicit
owner instruction and an SRS revision; update this test only together with
the SRS document.
"""
from __future__ import annotations

from config_microcapital import CORE_SRS_CONFIG, get_policy_config

SRS_V1_LOCKED = {
    "BASE_RISK_PCT": 0.032,
    "MAX_SIMULTANEOUS_TRADES": 2,
    "MAX_COMBINED_EXPOSURE": 0.05,
    "DAILY_STOP_LOSS_PCT": 0.08,
    "WEEKLY_STOP_LOSS_PCT": 0.15,
    "HARD_DRAWDOWN_PCT": 0.20,
    "LOSS_HALT_THRESHOLD": 3,
    "MIN_RISK_REWARD": 2.2,
}


def test_core_srs_config_matches_locked_srs_v1_constants():
    for key, expected in SRS_V1_LOCKED.items():
        assert CORE_SRS_CONFIG[key] == expected, f"{key} changed from SRS v1 value {expected}"


def test_default_runtime_policy_is_core_srs():
    policy = get_policy_config(env={})
    assert policy["MODE_ID"] == "core_srs"
    for key, expected in SRS_V1_LOCKED.items():
        assert policy[key] == expected, f"{key} differs in the default runtime policy"
