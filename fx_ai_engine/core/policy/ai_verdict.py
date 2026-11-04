"""Typed AI verdict and the reduce-only combinator (AGENTS.md invariant 3).

An AI component may keep or reduce the risk the rules approved, or reject the
signal. It may never approve a signal the rules rejected, size a trade above
the rule risk, or loosen a limit. ``apply_ai_verdict`` is the only way an AI
verdict is combined with a rule decision, so the invariant is enforced in one
place and covered by property tests (``tests/test_ai_invariant_properties.py``).

In v1 the result is logged only (shadow mode); routing never reads it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

SOURCE_META_LABELER_SHADOW = "meta_labeler_shadow"

AI_VERDICT_INVALID = "AI_VERDICT_INVALID"
AI_RULE_REJECTED = "AI_RULE_REJECTED"
AI_RULE_RISK_INVALID = "AI_RULE_RISK_INVALID"
AI_RISK_KEPT = "AI_RISK_KEPT"
AI_RISK_REDUCED = "AI_RISK_REDUCED"
AI_RISK_REDUCED_TO_ZERO = "AI_RISK_REDUCED_TO_ZERO"


@dataclass(frozen=True)
class AiVerdict:
    veto: bool
    risk_multiplier: float
    reason_code: str
    source: str


def verdict_from_shadow(decision: Any) -> AiVerdict:
    """Map a ``ShadowRuntimeDecision`` to a verdict.

    REJECT -> veto; PASS -> keep (multiplier 1.0); DISABLED/BYPASS -> no-op
    (multiplier 1.0, no veto). Any other outcome is invalid and fails closed
    (veto with ``AI_VERDICT_INVALID``).
    """
    outcome = str(getattr(decision, "outcome", "") or "").strip().upper()
    reason = str(getattr(decision, "reason_code", "") or "").strip()
    if outcome == "REJECT":
        return AiVerdict(True, 0.0, reason or "ML_SHADOW_REJECT", SOURCE_META_LABELER_SHADOW)
    if outcome in {"PASS", "DISABLED", "BYPASS"}:
        return AiVerdict(False, 1.0, reason or f"ML_SHADOW_{outcome}", SOURCE_META_LABELER_SHADOW)
    return AiVerdict(True, 0.0, AI_VERDICT_INVALID, SOURCE_META_LABELER_SHADOW)


def _finite_real(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def apply_ai_verdict(rule_approved: bool, rule_risk_percent: float, verdict: Any) -> tuple[bool, float, str]:
    """Combine a rule decision with an AI verdict; the result never exceeds the rule decision.

    Returns ``(approved, risk_percent, reason_code)``:
      * rule rejected -> ``(False, 0.0, AI_RULE_REJECTED)`` whatever the verdict says;
      * rule risk not a finite number >= 0 -> ``(False, 0.0, AI_RULE_RISK_INVALID)``;
      * verdict not an ``AiVerdict``, ``veto`` not a bool, or a non-finite or
        non-numeric multiplier -> ``(False, 0.0, AI_VERDICT_INVALID)`` (fail closed);
      * veto -> ``(False, 0.0, verdict.reason_code)``;
      * otherwise risk = rule risk x multiplier clamped to [0, 1]; a result of
        0 is a rejection (``AI_RISK_REDUCED_TO_ZERO``).
    """
    if rule_approved is not True:
        return False, 0.0, AI_RULE_REJECTED
    rule_risk = _finite_real(rule_risk_percent)
    if rule_risk is None or rule_risk < 0.0:
        return False, 0.0, AI_RULE_RISK_INVALID
    if not isinstance(verdict, AiVerdict) or not isinstance(verdict.veto, bool):
        return False, 0.0, AI_VERDICT_INVALID
    multiplier = _finite_real(verdict.risk_multiplier)
    if multiplier is None:
        return False, 0.0, AI_VERDICT_INVALID
    if verdict.veto:
        return False, 0.0, str(verdict.reason_code or AI_VERDICT_INVALID)
    multiplier = min(1.0, max(0.0, multiplier))
    risk = rule_risk * multiplier
    if risk <= 0.0:
        return False, 0.0, AI_RISK_REDUCED_TO_ZERO
    return True, min(risk, rule_risk), AI_RISK_KEPT if multiplier == 1.0 else AI_RISK_REDUCED
