"""The EA's default ExpectedPolicyHash must match the core_srs policy.

If the policy changes, update the pin in mt5_ea/FX_Execution.mq5, recompile,
and redeploy; otherwise every signal is rejected with REJECTED_POLICY_HASH.
"""
from __future__ import annotations

import re
from pathlib import Path

from config_microcapital import get_policy_config
from core.policy.envelope import canonical_policy_hash

EA_SOURCE = Path(__file__).parents[1] / "mt5_ea" / "FX_Execution.mq5"


def test_ea_default_policy_hash_matches_core_srs_policy():
    source = EA_SOURCE.read_text(encoding="utf-8")
    match = re.search(r'input string ExpectedPolicyHash = "([0-9a-f]*)";', source)
    assert match, "ExpectedPolicyHash input not found in EA source"
    assert match.group(1) == canonical_policy_hash(get_policy_config(mode_id="core_srs", env={}))
