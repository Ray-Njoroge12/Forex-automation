"""Approval-file schema and the pure checks of the live gate (core/live_approval.py)."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import live_approval
from core.live_approval import LiveApprovalError
from tests.live_fixtures import (
    DROP,
    EA_VERSION,
    LOGIN,
    MICRO_LIVE_HASH,
    approval_payload,
    file_sha256,
    write_approval,
    write_json,
    write_verdict,
)

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
ENGINE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_DIR.parent

REQUIRED_FIELDS = [
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
]


def _load(tmp_path, **overrides):
    path = tmp_path / "approval.json"
    write_approval(path, NOW, **overrides)
    return live_approval.load_approval(path, NOW)


def _expect(code, tmp_path, **overrides):
    with pytest.raises(LiveApprovalError) as info:
        _load(tmp_path, **overrides)
    assert info.value.code == code, info.value


def _approval(**overrides):
    return live_approval.parse_approval(json.dumps(approval_payload(NOW, **overrides)).encode(), NOW)


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------
def test_valid_file_loads_and_reports_file_sha256(tmp_path):
    approval, sha = _load(tmp_path, evidence_waiver={"reason": "r", "accepted_by": "a"})
    assert sha == file_sha256(tmp_path / "approval.json")
    assert approval.account_login == LOGIN
    assert approval.policy_hash == MICRO_LIVE_HASH
    assert approval.max_orders_per_day == 4
    assert approval.has_waiver is True


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(LiveApprovalError) as info:
        live_approval.load_approval(tmp_path / "nope.json", NOW)
    assert info.value.code == "LIVE_APPROVAL_MISSING"


def test_not_json_is_refused(tmp_path):
    path = tmp_path / "approval.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LiveApprovalError) as info:
        live_approval.load_approval(path, NOW)
    assert info.value.code == "LIVE_APPROVAL_MALFORMED"


def test_json_array_is_refused(tmp_path):
    path = tmp_path / "approval.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(LiveApprovalError) as info:
        live_approval.load_approval(path, NOW)
    assert info.value.code == "LIVE_APPROVAL_MALFORMED"


def test_oversized_file_is_refused(tmp_path):
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(approval_payload(NOW)) + " " * (live_approval.MAX_APPROVAL_BYTES + 1), encoding="utf-8")
    with pytest.raises(LiveApprovalError) as info:
        live_approval.load_approval(path, NOW)
    assert info.value.code == "LIVE_APPROVAL_MALFORMED"


def test_duplicate_keys_are_refused():
    raw = json.dumps(approval_payload(NOW)).replace('"approved_by": "Owner"', '"approved_by": "Owner", "approved_by": "Other"')
    with pytest.raises(LiveApprovalError) as info:
        live_approval.parse_approval(raw.encode(), NOW)
    assert info.value.code == "LIVE_APPROVAL_MALFORMED"


def test_non_finite_number_is_refused():
    raw = json.dumps(approval_payload(NOW)).replace('"max_lot_per_order": 0.01', '"max_lot_per_order": Infinity')
    with pytest.raises(LiveApprovalError) as info:
        live_approval.parse_approval(raw.encode(), NOW)
    assert info.value.code == "LIVE_APPROVAL_MALFORMED"


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_each_required_field_is_required(tmp_path, field):
    _expect("LIVE_APPROVAL_MALFORMED", tmp_path, **{field: DROP})


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": "1"},
        {"approved_by": ""},
        {"approved_by": 5},
        {"account_login": "7001234567"},
        {"account_login": 0},
        {"account_login": -4},
        {"account_login": 1.5},
        {"account_server": "  "},
        {"policy_hash": "abc"},
        {"policy_hash": MICRO_LIVE_HASH.upper()},
        {"policy_hash": 7},
        {"strategy_id": ""},
        {"ea_version_min": "test"},
        {"ea_version_min": ""},
        {"starting_balance_usd": "10"},
        {"starting_balance_usd": 0},
        {"starting_balance_usd": -1},
        {"max_total_loss_usd": "3"},
        {"max_total_loss_usd": 0},
        {"max_lot_per_order": "0.01"},
        {"max_lot_per_order": 0},
        {"max_orders_per_day": 0},
        {"max_orders_per_day": 2.5},
        {"max_orders_per_day": "4"},
        {"unexpected_field": 1},
    ],
)
def test_wrong_type_or_value_is_refused(tmp_path, overrides):
    _expect("LIVE_APPROVAL_MALFORMED", tmp_path, **overrides)


@pytest.mark.parametrize(
    "field,value",
    [
        ("starting_balance_usd", True),
        ("max_total_loss_usd", True),
        ("max_lot_per_order", True),
        ("max_orders_per_day", True),
        ("account_login", True),
    ],
)
def test_bool_is_not_a_number(tmp_path, field, value):
    _expect("LIVE_APPROVAL_MALFORMED", tmp_path, **{field: value})


@pytest.mark.parametrize("field", ["approved_at_utc", "expires_at_utc"])
def test_naive_timestamp_is_refused(tmp_path, field):
    _expect("LIVE_APPROVAL_MALFORMED", tmp_path, **{field: "2026-11-30T09:00:00"})


def test_garbage_timestamp_is_refused(tmp_path):
    _expect("LIVE_APPROVAL_MALFORMED", tmp_path, expires_at_utc="next week")


def test_expiry_not_after_approval_is_refused(tmp_path):
    same = (NOW - timedelta(hours=1)).isoformat()
    _expect("LIVE_APPROVAL_MALFORMED", tmp_path, approved_at_utc=same, expires_at_utc=same)


def test_wrong_policy_mode_is_refused(tmp_path):
    _expect("LIVE_APPROVAL_MALFORMED", tmp_path, policy_mode="core_srs")


def test_expired_approval_is_refused(tmp_path):
    _expect("LIVE_APPROVAL_EXPIRED", tmp_path, expires_at_utc=(NOW - timedelta(seconds=1)).isoformat())


def test_approval_expiring_exactly_now_is_expired(tmp_path):
    _expect("LIVE_APPROVAL_EXPIRED", tmp_path, expires_at_utc=NOW.isoformat())


def test_underscore_keys_are_ignored(tmp_path):
    approval, _ = _load(tmp_path, _note="free text", _field_notes={"a": "b"})
    assert approval.account_login == LOGIN


def test_malformed_beats_expired(tmp_path):
    _expect("LIVE_APPROVAL_MALFORMED", tmp_path, expires_at_utc=(NOW - timedelta(days=1)).isoformat(), account_login=0)


def test_example_file_is_refused_and_every_field_has_a_note():
    example = ENGINE_DIR / "config" / "live_approval.example.json"
    raw = example.read_bytes()
    with pytest.raises(LiveApprovalError) as info:
        live_approval.parse_approval(raw, datetime.now(timezone.utc))
    assert info.value.code in {"LIVE_APPROVAL_EXPIRED", "LIVE_APPROVAL_MALFORMED"}
    data = json.loads(raw)
    notes = data["_field_notes"]
    schema_fields = set(live_approval._REQUIRED_FIELDS) | set(live_approval._OPTIONAL_FIELDS)
    assert schema_fields <= set(notes), sorted(schema_fields - set(notes))
    assert "relative" in notes["evidence"].lower() and "fx_ai_engine" in notes["evidence"]
    assert "_alternative_evidence_waiver" in data


def test_example_file_has_no_live_value_that_could_pass():
    data = json.loads((ENGINE_DIR / "config" / "live_approval.example.json").read_text(encoding="utf-8"))
    assert data["approved_by"] == "REPLACE_ME"
    assert data["policy_hash"] != MICRO_LIVE_HASH
    assert datetime.fromisoformat(data["expires_at_utc"]) < datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_gitignore_has_the_approval_line_and_git_agrees():
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "fx_ai_engine/config/live_approval.json" in lines
    ignored = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "fx_ai_engine/config/live_approval.json"],
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0
    example = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "fx_ai_engine/config/live_approval.example.json"],
        capture_output=True,
        text=True,
    )
    assert example.returncode == 1


def test_default_approval_path_is_anchored_to_the_package():
    assert live_approval.DEFAULT_APPROVAL_PATH == ENGINE_DIR / "config" / "live_approval.json"
    assert live_approval.DEFAULT_APPROVAL_PATH.is_absolute()


# ----------------------------------------------------------------------
# Evidence
# ----------------------------------------------------------------------
def _evidence_approval(tmp_path, verdict_overrides=None, *, sha=None, path=None, **extra):
    verdict_path = tmp_path / "verdict.json"
    verdict_sha = write_verdict(verdict_path, **(verdict_overrides or {}))
    return _approval(
        evidence={"verdict_path": str(path or verdict_path), "sha256": sha or verdict_sha},
        **extra,
    )


def test_valid_verdict_passes(tmp_path):
    assert live_approval.check_evidence(_evidence_approval(tmp_path)) == "verdict"


def test_relative_verdict_path_resolves_against_the_engine_folder(tmp_path):
    verdict_path = tmp_path / "rel" / "verdict.json"
    sha = write_verdict(verdict_path)
    approval = _approval(evidence={"verdict_path": "rel/verdict.json", "sha256": sha})
    assert live_approval.check_evidence(approval, base_dir=tmp_path) == "verdict"
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(approval, base_dir=tmp_path / "elsewhere")
    assert info.value.code == "LIVE_EVIDENCE_FILE_MISSING"


def test_no_evidence_and_no_waiver_is_refused():
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(_approval())
    assert info.value.code == "LIVE_EVIDENCE_MISSING"


def test_missing_verdict_file_is_refused(tmp_path):
    approval = _evidence_approval(tmp_path, path=tmp_path / "gone.json")
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(approval)
    assert info.value.code == "LIVE_EVIDENCE_FILE_MISSING"


def test_sha_mismatch_is_refused(tmp_path):
    approval = _evidence_approval(tmp_path, sha="0" * 64)
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(approval)
    assert info.value.code == "LIVE_EVIDENCE_HASH_MISMATCH"


def test_verdict_fail_is_refused(tmp_path):
    approval = _evidence_approval(tmp_path, {"verdict": "FAIL"})
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(approval)
    assert info.value.code == "LIVE_EVIDENCE_NOT_PASS"


def test_not_evidence_status_is_refused(tmp_path):
    approval = _evidence_approval(tmp_path, {"evidence_status": "NOT_EVIDENCE"})
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(approval)
    assert info.value.code == "LIVE_EVIDENCE_NOT_ELIGIBLE"


def test_evidence_status_word_evidence_alone_is_refused(tmp_path):
    approval = _evidence_approval(tmp_path, {"evidence_status": "EVIDENCE"})
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(approval)
    assert info.value.code == "LIVE_EVIDENCE_NOT_ELIGIBLE"


def test_verdict_policy_hash_difference_is_refused(tmp_path):
    approval = _evidence_approval(tmp_path, {"policy_hash": "f" * 64})
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(approval)
    assert info.value.code == "LIVE_EVIDENCE_POLICY_MISMATCH"


def test_verdict_that_is_not_json_is_refused(tmp_path):
    verdict_path = tmp_path / "verdict.json"
    verdict_path.write_text("PASS", encoding="utf-8")
    approval = _approval(evidence={"verdict_path": str(verdict_path), "sha256": file_sha256(verdict_path)})
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(approval)
    assert info.value.code == "LIVE_EVIDENCE_NOT_PASS"


def test_waiver_route_is_accepted():
    approval = _approval(evidence_waiver={"reason": "no replay yet", "accepted_by": "Owner"})
    assert live_approval.check_evidence(approval) == "waiver"


def test_waiver_takes_precedence_over_failing_evidence(tmp_path):
    approval = _evidence_approval(
        tmp_path, {"verdict": "FAIL"}, evidence_waiver={"reason": "accepted", "accepted_by": "Owner"}
    )
    assert live_approval.check_evidence(approval) == "waiver"


@pytest.mark.parametrize(
    "waiver",
    [{"reason": "", "accepted_by": "Owner"}, {"reason": "why", "accepted_by": "  "}, {"reason": "", "accepted_by": ""}],
)
def test_empty_waiver_fields_do_not_count(waiver):
    approval = _approval(evidence_waiver=waiver)
    assert approval.has_waiver is False
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(approval)
    assert info.value.code == "LIVE_EVIDENCE_MISSING"


def test_empty_waiver_falls_back_to_the_evidence_route(tmp_path):
    approval = _evidence_approval(tmp_path, {"verdict": "FAIL"}, evidence_waiver={"reason": "", "accepted_by": "x"})
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_evidence(approval)
    assert info.value.code == "LIVE_EVIDENCE_NOT_PASS"


@pytest.mark.parametrize("waiver", [{"reason": "r"}, {"reason": 1, "accepted_by": "a"}, "text", {"reason": "r", "accepted_by": "a", "x": 1}])
def test_malformed_waiver_object_is_refused(tmp_path, waiver):
    _expect("LIVE_APPROVAL_MALFORMED", tmp_path, evidence_waiver=waiver)


@pytest.mark.parametrize(
    "evidence",
    [{"verdict_path": "x"}, {"verdict_path": "x", "sha256": "short"}, {"verdict_path": "", "sha256": "0" * 64}, "text"],
)
def test_malformed_evidence_object_is_refused(tmp_path, evidence):
    _expect("LIVE_APPROVAL_MALFORMED", tmp_path, evidence=evidence)


# ----------------------------------------------------------------------
# Confirm text
# ----------------------------------------------------------------------
def test_confirm_text_format():
    assert live_approval.expected_confirm_text(7001234567) == "I-ACCEPT-LIVE-RISK-7001234567"


def test_exact_confirm_text_passes():
    live_approval.check_confirm("I-ACCEPT-LIVE-RISK-7001234567", 7001234567)


@pytest.mark.parametrize(
    "provided",
    [
        None,
        "",
        "i-accept-live-risk-7001234567",
        "I-ACCEPT-LIVE-RISK-7001234567 ",
        " I-ACCEPT-LIVE-RISK-7001234567",
        "I-ACCEPT-LIVE-RISK-7001234568",
        "I-ACCEPT-LIVE-RISK-",
        "I-ACCEPT-LIVE-RISK",
        "yes",
    ],
)
def test_near_miss_confirm_text_is_refused(provided):
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_confirm(provided, 7001234567)
    assert info.value.code == "LIVE_CONFIRM_TEXT_WRONG"


# ----------------------------------------------------------------------
# EA version
# ----------------------------------------------------------------------
@pytest.mark.parametrize("actual", ["2026.11.04-2", "2026.11.04-3", "2026.11.05-1", "2027.01.01-1", "2026.11.04-10"])
def test_ea_version_at_or_above_minimum_passes(actual):
    live_approval.check_ea_version(actual, "2026.11.04-2")


@pytest.mark.parametrize("actual", ["2026.11.04-1", "2026.11.03-9", "2025.12.31-99"])
def test_ea_version_below_minimum_is_refused(actual):
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_ea_version(actual, "2026.11.04-2")
    assert info.value.code == "LIVE_EA_VERSION_TOO_OLD"


@pytest.mark.parametrize("actual", ["test", "", None, 20261104, "2026.13.01-1", "2026.11.04", "v2026.11.04-2"])
def test_unparsable_ea_version_is_refused(actual):
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_ea_version(actual, "2026.11.04-2")
    assert info.value.code == "LIVE_EA_VERSION_TOO_OLD"


def test_parse_ea_version_orders_build_numbers_numerically():
    assert live_approval.parse_ea_version("2026.11.04-10") > live_approval.parse_ea_version("2026.11.04-9")


# ----------------------------------------------------------------------
# live_guard
# ----------------------------------------------------------------------
def _status(**guard_changes):
    guard = {
        "account_real": True,
        "require_demo": False,
        "allow_live": True,
        "max_lot": 0.01,
        "max_orders_per_day": 4,
        "orders_today": 0,
        "kill_switch": False,
    }
    for key, value in guard_changes.items():
        if value is DROP:
            guard.pop(key, None)
        else:
            guard[key] = value
    return {"live_guard": guard, "ea_version": EA_VERSION}


def _guard_refused(status):
    with pytest.raises(LiveApprovalError) as info:
        live_approval.check_live_guard(status, _approval())
    assert info.value.code == "LIVE_GUARD_MISMATCH"


def test_equal_guard_passes():
    live_approval.check_live_guard(_status(), _approval())


def test_stricter_guard_passes():
    live_approval.check_live_guard(_status(max_lot=0.005, max_orders_per_day=2), _approval())


def test_looser_lot_is_refused():
    _guard_refused(_status(max_lot=0.02))


def test_looser_order_count_is_refused():
    _guard_refused(_status(max_orders_per_day=5))


@pytest.mark.parametrize("key", ["max_lot", "max_orders_per_day", "account_real", "allow_live", "kill_switch"])
def test_missing_guard_keys_are_refused(key):
    _guard_refused(_status(**{key: DROP}))


def test_missing_guard_block_is_refused():
    _guard_refused({"ea_version": EA_VERSION})
    _guard_refused({"live_guard": "yes"})


def test_allow_live_false_is_refused():
    _guard_refused(_status(allow_live=False))


def test_kill_switch_true_is_refused():
    _guard_refused(_status(kill_switch=True))


def test_account_real_false_is_refused():
    _guard_refused(_status(account_real=False))


@pytest.mark.parametrize("key,value", [("max_lot", "0.01"), ("max_orders_per_day", "4"), ("max_lot", True), ("max_orders_per_day", None)])
def test_non_numeric_guard_values_are_refused(key, value):
    _guard_refused(_status(**{key: value}))


@pytest.mark.parametrize("key,value", [("allow_live", 1), ("account_real", "true"), ("kill_switch", 0), ("kill_switch", None)])
def test_non_boolean_guard_flags_are_refused(key, value):
    _guard_refused(_status(**{key: value}))
