from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Mapping

SIGNAL_SCHEMA_VERSION = 2
DEFAULT_SIGNAL_TTL_SECONDS = 120


def canonical_policy_hash(policy: Mapping[str, object]) -> str:
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_utc(value: object) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def attach_execution_envelope(
    payload: Mapping[str, object],
    *,
    policy: Mapping[str, object],
    reservation_id: str,
    now_utc: datetime | None = None,
    ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS,
) -> dict[str, object]:
    now = now_utc or datetime.now(timezone.utc)
    ttl = max(1, int(ttl_seconds))
    result = dict(payload)
    result.update(
        {
            "schema_version": SIGNAL_SCHEMA_VERSION,
            "policy_hash": canonical_policy_hash(policy),
            "policy_mode": str(policy["MODE_ID"]),
            "reservation_id": reservation_id,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
            "max_spread_pips": float(policy["MAX_SPREAD_PIPS"]),
            "max_positions": int(policy["MAX_SIMULTANEOUS_TRADES"]),
            "max_combined_exposure": float(policy["MAX_COMBINED_EXPOSURE"]),
        }
    )
    return result


def validate_execution_envelope(
    payload: Mapping[str, object],
    *,
    now_utc: datetime | None = None,
    expected_policy_hash: str | None = None,
) -> None:
    required = {
        "schema_version",
        "policy_hash",
        "policy_mode",
        "reservation_id",
        "issued_at",
        "expires_at",
        "max_spread_pips",
        "max_positions",
        "max_combined_exposure",
    }
    missing = sorted(field for field in required if field not in payload)
    if missing:
        raise ValueError(f"Execution envelope missing fields: {missing}")
    if int(payload["schema_version"]) != SIGNAL_SCHEMA_VERSION:
        raise ValueError(f"Unsupported signal schema_version={payload['schema_version']}")
    if not str(payload["reservation_id"]).strip():
        raise ValueError("Execution envelope reservation_id must be non-empty")
    if float(payload["max_spread_pips"]) <= 0:
        raise ValueError("Execution envelope max_spread_pips must be > 0")
    if int(payload["max_positions"]) <= 0:
        raise ValueError("Execution envelope max_positions must be > 0")
    if not 0 < float(payload["max_combined_exposure"]) <= 1:
        raise ValueError("Execution envelope max_combined_exposure must be in (0, 1]")
    issued_at = _parse_utc(payload["issued_at"])
    expires_at = _parse_utc(payload["expires_at"])
    now = now_utc or datetime.now(timezone.utc)
    if expires_at <= issued_at:
        raise ValueError("Execution envelope expires_at must be after issued_at")
    if now > expires_at:
        raise ValueError("Execution envelope has expired")
    if issued_at - now > timedelta(seconds=5):
        raise ValueError("Execution envelope issued_at is in the future")
    if expected_policy_hash and str(payload["policy_hash"]) != expected_policy_hash:
        raise ValueError("Execution envelope policy_hash mismatch")
