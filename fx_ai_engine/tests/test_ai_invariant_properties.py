"""Property tests for AGENTS.md invariant 3 (AI can only keep or reduce risk, or reject).

Seeded ``random`` loops instead of ``hypothesis`` (not installed; no new
dependency). Each property runs over CASES generated inputs, deterministic in CI.
"""
from __future__ import annotations

import math
import random
from types import SimpleNamespace

from core.policy.ai_verdict import (
    AI_RULE_REJECTED,
    AI_VERDICT_INVALID,
    AiVerdict,
    apply_ai_verdict,
    verdict_from_shadow,
)
from ml.meta_labeler.shadow_runtime import (
    CanaryRuntimeConfig,
    evaluate_canary_decision,
    preserve_primary_route_decision,
)

CASES = 2000
SEED = 20261104
OUTCOMES = ("PASS", "REJECT", "BYPASS", "DISABLED", "pass", "reject", "", "UNKNOWN", None, 7)


def _random_number(rng: random.Random):
    """Mostly plausible numbers plus the awkward ones: NaN, inf, negatives, huge, bool, str, None."""
    roll = rng.random()
    if roll < 0.55:
        return rng.uniform(0.0, 1.5)
    if roll < 0.65:
        return rng.uniform(-5.0, 0.0)
    if roll < 0.72:
        return rng.uniform(1.0, 1e6)
    return rng.choice([float("nan"), float("inf"), float("-inf"), 0, 1, True, False, "0.5", None, 0.0, 1.0])


def _random_verdict(rng: random.Random):
    roll = rng.random()
    if roll < 0.85:
        veto = rng.random() < 0.3 if rng.random() < 0.9 else rng.choice([0, 1, "yes", None])
        return AiVerdict(veto, _random_number(rng), rng.choice(["ML_SHADOW_PASS", "X", ""]), "test")
    if roll < 0.95:
        return verdict_from_shadow(SimpleNamespace(outcome=rng.choice(OUTCOMES), reason_code="R"))
    return rng.choice([None, "PASS", 1.0, SimpleNamespace(veto=False, risk_multiplier=1.0)])


def _random_case(rng: random.Random):
    rule_approved = rng.choice([True, False, True, True, 1, 0, None])
    return rule_approved, _random_number(rng), _random_verdict(rng)


def _cases():
    rng = random.Random(SEED)
    return [_random_case(rng) for _ in range(CASES)]


def test_ai_never_approves_what_the_rules_did_not_approve():
    for rule_approved, rule_risk, verdict in _cases():
        approved, _, _ = apply_ai_verdict(rule_approved, rule_risk, verdict)
        assert isinstance(approved, bool)
        if approved:
            assert rule_approved is True, (rule_approved, rule_risk, verdict)


def test_ai_risk_is_never_above_the_rule_risk_and_never_negative():
    for rule_approved, rule_risk, verdict in _cases():
        approved, risk, _ = apply_ai_verdict(rule_approved, rule_risk, verdict)
        assert isinstance(risk, float) and math.isfinite(risk)
        assert risk >= 0.0
        if approved:
            assert risk > 0.0 and risk <= float(rule_risk), (rule_risk, verdict, risk)
        else:
            assert risk == 0.0


def test_rule_rejected_always_gives_false_and_zero():
    rng = random.Random(SEED + 1)
    for _ in range(CASES):
        out = apply_ai_verdict(rng.choice([False, 0, None, "", 1, "True"]), _random_number(rng), _random_verdict(rng))
        assert out[:2] == (False, 0.0)
        assert out[2] == AI_RULE_REJECTED


def test_invalid_multiplier_is_a_veto():
    rng = random.Random(SEED + 2)
    bad = [float("nan"), float("inf"), float("-inf"), "0.5", None, True, False, [1.0], object()]
    for _ in range(CASES):
        verdict = AiVerdict(rng.random() < 0.5, rng.choice(bad), "X", "test")
        assert apply_ai_verdict(True, rng.uniform(0.001, 0.05), verdict) == (False, 0.0, AI_VERDICT_INVALID)


def test_valid_keep_and_reduce_verdicts():
    rng = random.Random(SEED + 3)
    for _ in range(CASES):
        rule_risk = rng.uniform(0.001, 0.05)
        multiplier = rng.uniform(0.0, 3.0)
        approved, risk, _ = apply_ai_verdict(True, rule_risk, AiVerdict(False, multiplier, "X", "test"))
        expected = rule_risk * min(1.0, multiplier)
        assert risk == (expected if expected > 0 else 0.0)
        assert approved is (expected > 0)


def test_verdict_from_shadow_mapping():
    assert verdict_from_shadow(SimpleNamespace(outcome="REJECT", reason_code="ML_SHADOW_REJECT")).veto is True
    for outcome in ("PASS", "BYPASS", "DISABLED"):
        verdict = verdict_from_shadow(SimpleNamespace(outcome=outcome, reason_code="R"))
        assert (verdict.veto, verdict.risk_multiplier) == (False, 1.0)
    for outcome in ("", "MAYBE", None):
        verdict = verdict_from_shadow(SimpleNamespace(outcome=outcome, reason_code="R"))
        assert (verdict.veto, verdict.reason_code) == (True, AI_VERDICT_INVALID)


def _random_string(rng: random.Random) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ_ abcxyz019"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))


def test_preserve_primary_route_decision_is_identity_for_any_outcome():
    rng = random.Random(SEED + 4)
    for _ in range(CASES):
        primary = rng.random() < 0.5
        outcome = rng.choice([*OUTCOMES[:4], _random_string(rng)])
        assert preserve_primary_route_decision(primary, outcome) is primary


def test_canary_never_blocks_a_rejected_route_and_never_creates_a_route():
    rng = random.Random(SEED + 5)
    stages = ("POST_HARD_RISK", "PRE_ROUTE", "post_hard_risk", "")
    for _ in range(CASES):
        config = CanaryRuntimeConfig(
            enabled=rng.random() < 0.7,
            mode=rng.choice(["strict", "soft", "off", _random_string(rng)]),
            enforce_stage=rng.choice(stages).upper(),
            kill_switch=rng.random() < 0.2,
        )
        primary = rng.random() < 0.5
        decision = evaluate_canary_decision(
            config,
            stage=rng.choice(stages),
            shadow_enabled=rng.random() < 0.7,
            shadow_outcome=rng.choice([*OUTCOMES[:4], _random_string(rng)]),
            primary_gate_route=primary,
        )
        if not primary:
            assert decision.block_route is False
        # decision_cycle routes only when primary and not blocked.
        routed = primary and not decision.block_route
        assert routed <= primary
