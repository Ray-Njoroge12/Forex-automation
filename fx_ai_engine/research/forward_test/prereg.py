"""R10 pre-registration: a versioned go/no-go and abort criteria file, sealed by hash before the start.

The criteria file (JSON, schema ``fx-forward-criteria/1``) is written first. ``seal``
records its sha256 together with the forward-test start timestamp; the start
must be in the future when sealing unless ``--allow-backdate`` is given, and a
backdated seal is labelled as not pre-registered everywhere it is used.
``verify`` (also run by the evaluator) refuses when the file no longer matches
the sealed hash, with CRITERIA_MODIFIED_AFTER_START when its mtime is after the
start, or CRITERIA_HASH_MISMATCH otherwise.

The seal is only as trustworthy as where it is kept: commit the criteria file
and the seal before the start so the commit date anchors them.

Usage (from fx_ai_engine/):
    ../.venv/bin/python -m research.forward_test.prereg seal --criteria <file.json> --start 2026-12-01T00:00:00Z --out <seal.json>
    ../.venv/bin/python -m research.forward_test.prereg verify --criteria <file.json> --seal <seal.json>
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from research.forward_test.freeze import canonical_digest, sha256_file

CRITERIA_SCHEMA = "fx-forward-criteria/1"
SEAL_SCHEMA = "fx-forward-seal/1"
STATUSES = ("DRAFT_NOT_APPROVED", "APPROVED")
CRITERION_STATUSES = ("PROPOSED_IN_PLAN_NOT_APPROVED", "SRS_V1_UNCHANGED", "OPEN_OWNER_DECISION", "APPROVED")
OPS = (">=", ">", "<=", "<", "==")
# Metric names the evaluator computes (research.forward_test.evaluate.compute_metrics).
METRICS = (
    "closed_trades", "trading_days_elapsed", "win_rate", "avg_r_all", "avg_r_winners_srs",
    "expectancy_r", "expectancy_r_ci95_low", "expectancy_r_ci95_high", "profit_factor",
    "avg_winner_r", "avg_loser_r", "max_consecutive_losses", "max_drawdown", "unresolved_feedback_conflicts",
)


class PreregError(ValueError):
    """Raised with a reason code as the first word of the message."""


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def validate_criteria(criteria: Mapping[str, Any]) -> list[str]:
    """Return a list of problems; empty means the file is well formed."""
    errors: list[str] = []
    if criteria.get("schema") != CRITERIA_SCHEMA:
        return [f"schema must be {CRITERIA_SCHEMA!r}, got {criteria.get('schema')!r}"]
    for key in ("criteria_id", "criteria_version"):
        if not str(criteria.get(key) or "").strip():
            errors.append(f"{key} is required")
    status = criteria.get("status")
    if status not in STATUSES:
        errors.append(f"status must be one of {STATUSES}, got {status!r}")
    sample = criteria.get("sample") or {}
    for key in ("min_closed_trades", "min_trading_days"):
        if not isinstance(sample.get(key), int) or sample[key] < 0:
            errors.append(f"sample.{key} must be a non-negative integer")
    if not isinstance(criteria.get("scope"), dict):
        errors.append("scope must be an object")
    seen: set[str] = set()
    for group in ("go", "abort"):
        items = criteria.get(group)
        if not isinstance(items, list) or (group == "go" and not items):
            errors.append(f"{group} must be a list" + (" with at least one criterion" if group == "go" else ""))
            continue
        for c in items:
            cid = c.get("id")
            if not cid or cid in seen:
                errors.append(f"{group}: missing or duplicate id {cid!r}")
            seen.add(cid)
            if c.get("metric") not in METRICS:
                errors.append(f"{cid}: unknown metric {c.get('metric')!r}")
            if c.get("op") not in OPS:
                errors.append(f"{cid}: op must be one of {OPS}")
            if c.get("status") not in CRITERION_STATUSES:
                errors.append(f"{cid}: status must be one of {CRITERION_STATUSES}")
            value = c.get("value")
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                errors.append(f"{cid}: value must be a number or null")
            if value is None and c.get("status") != "OPEN_OWNER_DECISION":
                errors.append(f"{cid}: value is null but status is not OPEN_OWNER_DECISION")
            if status == "APPROVED" and (value is None or c.get("status") != "APPROVED"):
                errors.append(f"{cid}: an APPROVED file cannot contain open or unapproved criteria")
            if not isinstance(c.get("min_trades", 0), int):
                errors.append(f"{cid}: min_trades must be an integer")
    if status == "APPROVED" and not criteria.get("approved_by"):
        errors.append("approved_by is required when status is APPROVED")
    return errors


def load_criteria(path: Path) -> dict[str, Any]:
    try:
        criteria = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreregError(f"CRITERIA_UNREADABLE {path}: {exc}") from exc
    errors = validate_criteria(criteria)
    if errors:
        raise PreregError("CRITERIA_INVALID " + "; ".join(errors))
    return criteria


def seal(criteria_path: Path, start_utc: str | datetime, *, now: datetime | None = None,
         allow_backdate: bool = False) -> dict[str, Any]:
    criteria = load_criteria(criteria_path)
    now_utc = parse_utc(now or datetime.now(timezone.utc))
    start = parse_utc(start_utc)
    backdated = start <= now_utc
    if backdated and not allow_backdate:
        raise PreregError(f"START_NOT_IN_FUTURE start {start.isoformat()} is not after now {now_utc.isoformat()}; "
                          "pre-registration must be sealed before the start (use --allow-backdate only for demos)")
    body = {
        "schema": SEAL_SCHEMA,
        "criteria_path": str(criteria_path),
        "criteria_sha256": sha256_file(criteria_path),
        "criteria_id": criteria["criteria_id"],
        "criteria_version": criteria["criteria_version"],
        "criteria_status": criteria["status"],
        "start_utc": start.isoformat(),
        "sealed_at_utc": now_utc.isoformat(),
        "backdated": backdated,
    }
    return {**body, "seal_sha256": canonical_digest(body)}


def verify(seal_record: Mapping[str, Any], criteria_path: Path) -> dict[str, Any]:
    """ok=False with reason codes means the evaluator must refuse."""
    reasons: list[str] = []
    warnings: list[str] = []
    out: dict[str, Any] = {"ok": False, "reasons": reasons, "warnings": warnings, "criteria": None,
                           "start_utc": seal_record.get("start_utc"), "criteria_path": str(criteria_path)}
    if seal_record.get("schema") != SEAL_SCHEMA:
        reasons.append("SEAL_SCHEMA_UNSUPPORTED")
        return out
    body = {k: v for k, v in seal_record.items() if k != "seal_sha256"}
    if canonical_digest(body) != seal_record.get("seal_sha256"):
        reasons.append("SEAL_DIGEST_MISMATCH")
        return out
    start = parse_utc(seal_record["start_utc"])
    sealed_at = parse_utc(seal_record["sealed_at_utc"])
    if sealed_at >= start and not seal_record.get("backdated"):
        reasons.append("SEAL_AFTER_START")
    path = Path(criteria_path)
    if not path.is_file():
        reasons.append("CRITERIA_MISSING")
        return out
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    out.update(criteria_sha256=sha256_file(path), sealed_sha256=seal_record["criteria_sha256"],
               criteria_mtime_utc=mtime.isoformat())
    if out["criteria_sha256"] != seal_record["criteria_sha256"]:
        reasons.append("CRITERIA_MODIFIED_AFTER_START" if mtime > start else "CRITERIA_HASH_MISMATCH")
    elif mtime > start:
        warnings.append("CRITERIA_TOUCHED_AFTER_START: mtime is after the start but the content hash is unchanged")
    try:
        criteria = load_criteria(path)
    except PreregError as exc:
        reasons.append(str(exc).split(" ", 1)[0])
        return out
    out["criteria"] = criteria
    if seal_record.get("backdated"):
        warnings.append("BACKDATED_SEAL: sealed after the start; results are not pre-registered evidence")
    if criteria["status"] != "APPROVED":
        warnings.append(f"CRITERIA_{criteria['status']}: criteria are a draft; no go/no-go decision can rest on them")
    out["ok"] = not reasons
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R10 pre-registration: seal or verify a forward-test criteria file")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seal")
    s.add_argument("--criteria", type=Path, required=True)
    s.add_argument("--start", required=True, help="forward-test start, ISO 8601 UTC")
    s.add_argument("--out", type=Path, required=True, help="seal JSON to write")
    s.add_argument("--allow-backdate", action="store_true", help="demo only: allow a start that is not in the future")
    v = sub.add_parser("verify")
    v.add_argument("--criteria", type=Path, required=True)
    v.add_argument("--seal", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "seal":
        try:
            record = seal(args.criteria, args.start, allow_backdate=args.allow_backdate)
        except PreregError as exc:
            print(f"REFUSED: {exc}")
            return 2
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(f"Sealed {record['criteria_id']} v{record['criteria_version']} ({record['criteria_status']})")
        print(f"  criteria_sha256 {record['criteria_sha256']}")
        print(f"  start_utc {record['start_utc']}  sealed_at_utc {record['sealed_at_utc']}  backdated {record['backdated']}")
        print(f"  seal_sha256 {record['seal_sha256']} -> {args.out}")
        return 0
    result = verify(json.loads(args.seal.read_text(encoding="utf-8")), args.criteria)
    print(f"Pre-registration: {'OK' if result['ok'] else 'REFUSED'} {' '.join(result['reasons'])}".rstrip())
    for w in result["warnings"]:
        print(f"  warning: {w}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
