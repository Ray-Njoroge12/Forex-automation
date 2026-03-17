from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

LEGACY_UNPARTITIONED_STREAM = "legacy_unpartitioned"
LEGACY_UNPARTITIONED_ACCOUNT_SCOPE = "legacy_unpartitioned"


@dataclass(frozen=True)
class EvidenceContext:
    evidence_stream: str
    policy_mode: str
    execution_mode: str
    account_scope: str


def _sanitize_token(value: object) -> str:
    token = str(value or "unknown").strip() or "unknown"
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in token)


def build_runtime_evidence_context(
    policy: Mapping[str, object],
    *,
    use_mock: bool,
    login: int = 0,
    server: str = "",
) -> EvidenceContext:
    policy_mode = str(policy.get("MODE_ID", "unknown") or "unknown").strip().lower()
    experiment_tag = str(policy.get("EXPERIMENT_TAG", "") or "").strip().lower()
    experiment_token = _sanitize_token(experiment_tag) if experiment_tag else ""
    execution_mode = "mock" if use_mock else "mt5"
    account_scope = "mock" if use_mock else f"mt5:{_sanitize_token(server)}:{int(login) if login else 'unknown'}"
    evidence_stream = f"runtime_{execution_mode}_{policy_mode}"
    if experiment_token:
        evidence_stream = f"{evidence_stream}__{experiment_token}"
    return EvidenceContext(
        evidence_stream=evidence_stream,
        policy_mode=policy_mode,
        execution_mode=execution_mode,
        account_scope=account_scope,
    )