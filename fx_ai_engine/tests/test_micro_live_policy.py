"""micro_live policy mode (C3): CORE_SRS_CONFIG with a 1.5 % base risk.

The micro_live hash is pinned here. Changing any micro_live value or label
changes the hash and needs a new live approval file (see ADR 0013).
"""
from __future__ import annotations

import json

import pytest

import main
from config_microcapital import (
    CORE_SRS_CONFIG,
    MICRO_LIVE_CONFIG,
    MODE_CONFIGS,
    NON_SRS_MODE_IDS,
    OVERRIDE_ENABLED_MODE_IDS,
    get_policy_config,
    is_non_srs_policy_mode,
)
from core.policy.envelope import canonical_policy_hash
from validation import ea_demo_matrix, print_policy_hash

CORE_SRS_HASH = "f00f1f9c3e9120971378f3c1eb95d28f09d1d5e2ee97035a7f4c00a998535dc8"
MICRO_LIVE_HASH = "53ff41104c05c4c6374ad59d62023575d648155ce190a507a7530c6e112f9489"
OVERRIDDEN_KEYS = {"MODE_ID", "MODE_LABEL", "EVIDENCE_LABEL", "BASE_RISK_PCT"}


class _StubBridge:
    def __init__(self, is_demo):
        self._is_demo = is_demo

    def is_demo_account(self):
        return self._is_demo


def _micro_live_policy():
    return get_policy_config("micro_live", env={})


def test_micro_live_inherits_core_srs_except_four_keys():
    assert set(MICRO_LIVE_CONFIG) == set(CORE_SRS_CONFIG)
    for key, value in CORE_SRS_CONFIG.items():
        if key in OVERRIDDEN_KEYS:
            assert MICRO_LIVE_CONFIG[key] != value
        else:
            assert MICRO_LIVE_CONFIG[key] == value, key
    assert MICRO_LIVE_CONFIG["MODE_ID"] == "micro_live"
    assert MICRO_LIVE_CONFIG["MODE_LABEL"] == "Micro live"
    assert MICRO_LIVE_CONFIG["EVIDENCE_LABEL"] == "Micro live percent-risk"
    assert MICRO_LIVE_CONFIG["BASE_RISK_PCT"] == 0.015
    assert MICRO_LIVE_CONFIG["FIXED_RISK_USD"] is None
    assert MICRO_LIVE_CONFIG["MAX_SPREAD_PIPS"] == 2.0
    assert MICRO_LIVE_CONFIG["MAX_SIMULTANEOUS_TRADES"] == 2
    two_positions = MICRO_LIVE_CONFIG["BASE_RISK_PCT"] * MICRO_LIVE_CONFIG["MAX_SIMULTANEOUS_TRADES"]
    assert two_positions == pytest.approx(0.03)
    assert two_positions <= MICRO_LIVE_CONFIG["MAX_COMBINED_EXPOSURE"]
    assert MODE_CONFIGS["micro_live"] is MICRO_LIVE_CONFIG


def test_policy_hashes_are_pinned():
    assert canonical_policy_hash(get_policy_config("core_srs", env={})) == CORE_SRS_HASH
    assert canonical_policy_hash(_micro_live_policy()) == MICRO_LIVE_HASH


def test_micro_live_is_non_srs_and_has_no_env_overrides():
    assert is_non_srs_policy_mode("micro_live") is True
    assert "micro_live" in NON_SRS_MODE_IDS
    assert "micro_live" not in OVERRIDE_ENABLED_MODE_IDS


def test_env_overrides_do_not_move_micro_live_values():
    env = {
        "FX_POLICY_MODE": "micro_live",
        "FX_ALLOW_NON_SRS_POLICY": "1",
        "MAX_SPREAD_PIPS": "9",
        "FIXED_RISK_USD": "5",
    }
    policy = get_policy_config("micro_live", env=env)
    assert policy["MAX_SPREAD_PIPS"] == 2.0
    assert policy["FIXED_RISK_USD"] is None
    assert canonical_policy_hash(policy) == MICRO_LIVE_HASH


def test_pinned_quirk_override_decision_uses_env_mode_not_policy_mode():
    # Current behaviour, not a goal: the override gate reads FX_POLICY_MODE from
    # the env, so another override-enabled env mode lets MAX_SPREAD_PIPS move a
    # micro_live policy. The live gate defends against it (see ADR 0013).
    env = {
        "FX_POLICY_MODE": "preserve_10",
        "FX_ALLOW_NON_SRS_POLICY": "1",
        "MAX_SPREAD_PIPS": "9",
    }
    policy = get_policy_config("micro_live", env=env)
    assert policy["MAX_SPREAD_PIPS"] == 9.0
    assert canonical_policy_hash(policy) != MICRO_LIVE_HASH


FLAG_ENV = {"FX_ALLOW_NON_SRS_POLICY": "1"}


def test_runtime_approval_mock_with_flag_is_approved():
    decision = main.evaluate_runtime_policy_approval(
        _StubBridge(None), policy=_micro_live_policy(), use_mock=True, env=FLAG_ENV
    )
    assert decision.approved is True


def test_runtime_approval_demo_with_flag_is_approved():
    decision = main.evaluate_runtime_policy_approval(
        _StubBridge(True), policy=_micro_live_policy(), use_mock=False, env=FLAG_ENV
    )
    assert decision.approved is True


def test_runtime_approval_real_account_with_flag_is_rejected():
    decision = main.evaluate_runtime_policy_approval(
        _StubBridge(False), policy=_micro_live_policy(), use_mock=False, env=FLAG_ENV
    )
    assert decision.approved is False
    assert decision.reason_code == "NON_SRS_POLICY_REQUIRES_MOCK_OR_DEMO"


def test_runtime_approval_without_flag_is_rejected():
    decision = main.evaluate_runtime_policy_approval(
        _StubBridge(True), policy=_micro_live_policy(), use_mock=True, env={}
    )
    assert decision.approved is False
    assert decision.reason_code == "NON_SRS_POLICY_EXPLICIT_APPROVAL_REQUIRED"


def test_print_policy_hash_pin_and_cli(capsys):
    pin = print_policy_hash.build_policy_pin("micro_live")
    assert pin == {"mode_id": "micro_live", "policy_hash": MICRO_LIVE_HASH}
    assert print_policy_hash.main(["--mode", "micro_live", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == pin


def test_demo_matrix_smoke_for_micro_live(tmp_path):
    manifest_path = ea_demo_matrix.write_demo_matrix(tmp_path, mode_id="micro_live")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["policy_pin"]["policy_hash"] == MICRO_LIVE_HASH
    assert manifest["cases"]
