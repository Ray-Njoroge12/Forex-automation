"""Live approval file: schema, strict parsing and the pure checks of the live gate.

``--mode live`` starts only when the owner has written an approval file for one
account, one policy hash and one strategy, with an expiry and loss caps. This
module holds the schema and the pure checks. It reads files but never writes
anything and never touches the bridge folder. Every refusal is a
``LiveApprovalError`` carrying a reason code (see ADR 0013).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

LIVE_POLICY_MODE = "micro_live"
LIVE_STRATEGY_ID = "trend_pullback_v1"
APPROVAL_ENV = "FX_LIVE_APPROVAL_FILE"
CONFIRM_ENV = "FX_LIVE_CONFIRM"

ENGINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APPROVAL_PATH = ENGINE_ROOT / "config" / "live_approval.json"

APPROVAL_SCHEMA_VERSION = 1
MAX_APPROVAL_BYTES = 64 * 1024
MAX_VERDICT_BYTES = 16 * 1024 * 1024

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_EA_VERSION = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})-(\d+)$")

_REQUIRED_FIELDS = (
    "schema_version",
    "approved_by",
    "approved_at_utc",
    "expires_at_utc",
    "account_login",
    "account_server",
    "policy_mode",
    "policy_hash",
    "strategy_id",
    "ea_version_min",
    "starting_balance_usd",
    "max_total_loss_usd",
    "max_orders_per_day",
    "max_lot_per_order",
)
_OPTIONAL_FIELDS = ("evidence", "evidence_waiver")
_EVIDENCE_KEYS = ("verdict_path", "sha256")
_WAIVER_KEYS = ("reason", "accepted_by")


class LiveApprovalError(Exception):
    """A live-gate refusal with a stable reason code."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code} {detail}".strip())


@dataclass(frozen=True)
class LiveApproval:
    approved_by: str
    approved_at_utc: datetime
    expires_at_utc: datetime
    account_login: int
    account_server: str
    policy_mode: str
    policy_hash: str
    strategy_id: str
    ea_version_min: str
    starting_balance_usd: float
    max_total_loss_usd: float
    max_orders_per_day: int
    max_lot_per_order: float
    evidence_verdict_path: str | None = None
    evidence_sha256: str | None = None
    waiver_reason: str | None = None
    waiver_accepted_by: str | None = None

    @property
    def has_waiver(self) -> bool:
        """True when the waiver route is complete (both fields non-empty)."""
        return bool((self.waiver_reason or "").strip() and (self.waiver_accepted_by or "").strip())


# ----------------------------------------------------------------------
# Strict value helpers
# ----------------------------------------------------------------------
def _malformed(detail: str) -> LiveApprovalError:
    return LiveApprovalError("LIVE_APPROVAL_MALFORMED", detail)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _str(data: Mapping[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise _malformed(f"{key} must be a non-empty string")
    return value


def _positive_number(data: Mapping[str, Any], key: str) -> float:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise _malformed(f"{key} must be a finite number greater than 0")
    return float(value)


def _positive_int(data: Mapping[str, Any], key: str) -> int:
    value = data[key]
    if type(value) is not int or value < 1:
        raise _malformed(f"{key} must be an integer >= 1")
    return value


def _timestamp(data: Mapping[str, Any], key: str) -> datetime:
    value = data[key]
    if not isinstance(value, str):
        raise _malformed(f"{key} must be an ISO 8601 string with a timezone")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise _malformed(f"{key} is not a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _malformed(f"{key} has no timezone")
    return parsed.astimezone(timezone.utc)


def _hex64(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise _malformed(f"{label} must be 64 lowercase hex characters")
    return value


def _sub_object(data: Mapping[str, Any], key: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    value = data[key]
    if not isinstance(value, dict):
        raise _malformed(f"{key} must be an object")
    clean = {k: v for k, v in value.items() if not (isinstance(k, str) and k.startswith("_"))}
    unknown = sorted(set(clean) - set(allowed))
    missing = [k for k in allowed if k not in clean]
    if unknown or missing:
        raise _malformed(f"{key} needs exactly {list(allowed)} (unknown={unknown} missing={missing})")
    return clean


# ----------------------------------------------------------------------
# Load and parse
# ----------------------------------------------------------------------
def read_approval_bytes(path: Path | str) -> bytes:
    """Read the approval file bytes (size capped). Missing file -> LIVE_APPROVAL_MISSING."""
    target = Path(path)
    try:
        if not target.is_file():
            raise LiveApprovalError("LIVE_APPROVAL_MISSING", f"approval file not found: {target}")
        if target.stat().st_size > MAX_APPROVAL_BYTES:
            raise _malformed(f"approval file larger than {MAX_APPROVAL_BYTES} bytes")
        raw = target.read_bytes()
    except LiveApprovalError:
        raise
    except OSError as exc:
        raise _malformed(f"approval file unreadable: {exc}") from exc
    if len(raw) > MAX_APPROVAL_BYTES:
        raise _malformed(f"approval file larger than {MAX_APPROVAL_BYTES} bytes")
    return raw


def parse_approval(raw: bytes, now: datetime) -> LiveApproval:
    """Validate approval bytes strictly. Raises LIVE_APPROVAL_MALFORMED or LIVE_APPROVAL_EXPIRED."""
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise _malformed(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _malformed("top level must be a JSON object")
    data = {k: v for k, v in data.items() if not (isinstance(k, str) and k.startswith("_"))}
    unknown = sorted(set(data) - set(_REQUIRED_FIELDS) - set(_OPTIONAL_FIELDS))
    if unknown:
        raise _malformed(f"unknown fields: {unknown}")
    missing = [key for key in _REQUIRED_FIELDS if key not in data]
    if missing:
        raise _malformed(f"missing fields: {missing}")

    if type(data["schema_version"]) is not int or data["schema_version"] != APPROVAL_SCHEMA_VERSION:
        raise _malformed(f"schema_version must be the integer {APPROVAL_SCHEMA_VERSION}")
    login = data["account_login"]
    if type(login) is not int or login <= 0:
        raise _malformed("account_login must be a positive integer")
    policy_mode = _str(data, "policy_mode")
    if policy_mode != LIVE_POLICY_MODE:
        raise _malformed(f"policy_mode must be {LIVE_POLICY_MODE!r}")
    approved_at = _timestamp(data, "approved_at_utc")
    expires_at = _timestamp(data, "expires_at_utc")
    if expires_at <= approved_at:
        raise _malformed("expires_at_utc must be after approved_at_utc")
    ea_version_min = _str(data, "ea_version_min")
    if parse_ea_version(ea_version_min) is None:
        raise _malformed("ea_version_min must look like YYYY.MM.DD-N")

    evidence_path = evidence_sha = waiver_reason = waiver_by = None
    if "evidence" in data:
        evidence = _sub_object(data, "evidence", _EVIDENCE_KEYS)
        evidence_path = evidence["verdict_path"]
        if not isinstance(evidence_path, str) or not evidence_path.strip():
            raise _malformed("evidence.verdict_path must be a non-empty string")
        evidence_sha = _hex64(evidence["sha256"], "evidence.sha256")
    if "evidence_waiver" in data:
        waiver = _sub_object(data, "evidence_waiver", _WAIVER_KEYS)
        waiver_reason, waiver_by = waiver["reason"], waiver["accepted_by"]
        if not isinstance(waiver_reason, str) or not isinstance(waiver_by, str):
            raise _malformed("evidence_waiver.reason and accepted_by must be strings")

    approval = LiveApproval(
        approved_by=_str(data, "approved_by"),
        approved_at_utc=approved_at,
        expires_at_utc=expires_at,
        account_login=login,
        account_server=_str(data, "account_server"),
        policy_mode=policy_mode,
        policy_hash=_hex64(data["policy_hash"], "policy_hash"),
        strategy_id=_str(data, "strategy_id"),
        ea_version_min=ea_version_min,
        starting_balance_usd=_positive_number(data, "starting_balance_usd"),
        max_total_loss_usd=_positive_number(data, "max_total_loss_usd"),
        max_orders_per_day=_positive_int(data, "max_orders_per_day"),
        max_lot_per_order=_positive_number(data, "max_lot_per_order"),
        evidence_verdict_path=evidence_path,
        evidence_sha256=evidence_sha,
        waiver_reason=waiver_reason,
        waiver_accepted_by=waiver_by,
    )
    if approval.expires_at_utc <= now:
        raise LiveApprovalError(
            "LIVE_APPROVAL_EXPIRED",
            f"expired at {approval.expires_at_utc.isoformat()} (now {now.isoformat()})",
        )
    return approval


def load_approval(path: Path | str, now: datetime) -> tuple[LiveApproval, str]:
    """Return the validated approval and the sha256 hex of the file bytes."""
    raw = read_approval_bytes(path)
    return parse_approval(raw, now), hashlib.sha256(raw).hexdigest()


# ----------------------------------------------------------------------
# Confirm text
# ----------------------------------------------------------------------
def expected_confirm_text(login: int) -> str:
    return f"I-ACCEPT-LIVE-RISK-{login}"


def check_confirm(provided: object, login: int) -> None:
    """Exact match only: no trimming, no case folding."""
    if not isinstance(provided, str) or provided != expected_confirm_text(login):
        raise LiveApprovalError("LIVE_CONFIRM_TEXT_WRONG", "FX_LIVE_CONFIRM does not match the required text")


# ----------------------------------------------------------------------
# Evidence
# ----------------------------------------------------------------------
def check_evidence(approval: LiveApproval, *, base_dir: Path | None = None) -> str:
    """Return ``"waiver"`` or ``"verdict"`` when the evidence rule is satisfied.

    The waiver route wins when both routes are present and the waiver is
    complete. An incomplete waiver is ignored and the evidence route applies.
    """
    if approval.has_waiver:
        return "waiver"
    if approval.evidence_verdict_path is None or approval.evidence_sha256 is None:
        raise LiveApprovalError("LIVE_EVIDENCE_MISSING", "neither evidence nor a complete evidence_waiver is present")
    root = ENGINE_ROOT if base_dir is None else Path(base_dir)
    verdict_path = Path(approval.evidence_verdict_path)
    if not verdict_path.is_absolute():
        verdict_path = root / verdict_path
    try:
        if not verdict_path.is_file() or verdict_path.stat().st_size > MAX_VERDICT_BYTES:
            raise LiveApprovalError("LIVE_EVIDENCE_FILE_MISSING", f"verdict file unavailable: {verdict_path}")
        raw = verdict_path.read_bytes()
    except LiveApprovalError:
        raise
    except OSError as exc:
        raise LiveApprovalError("LIVE_EVIDENCE_FILE_MISSING", f"verdict file unreadable: {exc}") from exc
    if hashlib.sha256(raw).hexdigest() != approval.evidence_sha256:
        raise LiveApprovalError("LIVE_EVIDENCE_HASH_MISMATCH", "sha256 of the verdict file differs from the approval")
    try:
        verdict = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LiveApprovalError("LIVE_EVIDENCE_NOT_PASS", f"verdict file is not valid JSON: {exc}") from exc
    if not isinstance(verdict, dict) or verdict.get("verdict") != "PASS":
        raise LiveApprovalError("LIVE_EVIDENCE_NOT_PASS", "verdict is not PASS")
    if verdict.get("evidence_status") != "EVIDENCE_ELIGIBLE":
        raise LiveApprovalError("LIVE_EVIDENCE_NOT_ELIGIBLE", "evidence_status is not EVIDENCE_ELIGIBLE")
    if verdict.get("policy_hash") != approval.policy_hash:
        raise LiveApprovalError("LIVE_EVIDENCE_POLICY_MISMATCH", "verdict policy_hash differs from the approval")
    return "verdict"


# ----------------------------------------------------------------------
# EA version and live_guard
# ----------------------------------------------------------------------
def parse_ea_version(value: object) -> tuple[int, int, int, int] | None:
    """Parse ``YYYY.MM.DD-N`` into a sortable tuple, or ``None`` when it does not match."""
    if not isinstance(value, str):
        return None
    match = _EA_VERSION.match(value)
    if match is None:
        return None
    year, month, day, build = (int(part) for part in match.groups())
    try:
        date(year, month, day)
    except ValueError:
        return None
    return year, month, day, build


def check_ea_version(actual: object, minimum: str) -> None:
    parsed_actual = parse_ea_version(actual)
    parsed_min = parse_ea_version(minimum)
    if parsed_actual is None or parsed_min is None:
        raise LiveApprovalError("LIVE_EA_VERSION_TOO_OLD", f"unparsable EA version actual={actual!r} minimum={minimum!r}")
    if parsed_actual < parsed_min:
        raise LiveApprovalError("LIVE_EA_VERSION_TOO_OLD", f"EA version {actual} is older than {minimum}")


def _guard_number(guard: Mapping[str, Any], key: str) -> float:
    value = guard.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LiveApprovalError("LIVE_GUARD_MISMATCH", f"live_guard.{key} missing or not a number")
    return float(value)


def check_live_guard(status: Mapping[str, Any], approval: LiveApproval) -> None:
    """The EA must report a live guard that is at least as strict as the approval."""
    guard = status.get("live_guard") if isinstance(status, Mapping) else None
    if not isinstance(guard, Mapping):
        raise LiveApprovalError("LIVE_GUARD_MISMATCH", "live_guard block missing from EA status")
    if guard.get("account_real") is not True:
        raise LiveApprovalError("LIVE_GUARD_MISMATCH", "live_guard.account_real is not true")
    if guard.get("allow_live") is not True:
        raise LiveApprovalError("LIVE_GUARD_MISMATCH", "live_guard.allow_live is not true")
    if guard.get("kill_switch") is not False:
        raise LiveApprovalError("LIVE_GUARD_MISMATCH", "live_guard.kill_switch is not false")
    max_lot = _guard_number(guard, "max_lot")
    if max_lot > approval.max_lot_per_order:
        raise LiveApprovalError(
            "LIVE_GUARD_MISMATCH",
            f"EA max_lot {max_lot} exceeds approved max_lot_per_order {approval.max_lot_per_order}",
        )
    max_orders = _guard_number(guard, "max_orders_per_day")
    if max_orders > approval.max_orders_per_day:
        raise LiveApprovalError(
            "LIVE_GUARD_MISMATCH",
            f"EA max_orders_per_day {max_orders} exceeds approved {approval.max_orders_per_day}",
        )
