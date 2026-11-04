"""R10 pre-registration: criteria format, sealing before the start, refusal after modification."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research.forward_test import prereg
from validation import validate_demo

DRAFT = Path(__file__).parents[1] / "docs" / "research" / "forward-test" / "criteria-srs-v1.1-draft.json"
START = datetime(2027, 1, 4, tzinfo=timezone.utc)
BEFORE = START - timedelta(days=3)


@pytest.fixture()
def criteria(tmp_path):
    path = tmp_path / "criteria.json"
    shutil.copy(DRAFT, path)
    stamp = BEFORE.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _by_id(c):
    return {x["id"]: x for x in c["go"] + c["abort"]}


def test_starter_file_is_a_valid_draft_with_plan_and_srs_values():
    c = json.loads(DRAFT.read_text(encoding="utf-8"))
    assert prereg.validate_criteria(c) == []
    assert c["status"] == "DRAFT_NOT_APPROVED" and c["approved_by"] is None
    assert "NOT APPROVED" in c["notice"]
    items = _by_id(c)
    # Plan decision D2 recommended defaults, labelled as not approved.
    for cid, value in (("G1_EXPECTANCY", 0.25), ("G2_PROFIT_FACTOR", 1.3), ("G3_AVG_WINNER", 1.5)):
        assert items[cid]["value"] == value and items[cid]["status"] == "PROPOSED_IN_PLAN_NOT_APPROVED"
    # Unchanged SRS v1 lines match the constants validate_demo enforces today.
    assert items["G4_WIN_RATE"]["value"] == validate_demo.MIN_WIN_RATE
    assert items["G5_MAX_DRAWDOWN"]["value"] == validate_demo.MAX_DRAWDOWN
    assert items["A1_DRAWDOWN"]["value"] == validate_demo.ABORT_DRAWDOWN
    assert items["A2_WIN_RATE"]["value"] == validate_demo.ABORT_WIN_RATE
    assert c["sample"]["min_closed_trades"] == validate_demo.MIN_TRADES
    assert items["A3_AVG_R_REPLACEMENT"]["value"] is None
    assert items["A3_AVG_R_REPLACEMENT"]["status"] == "OPEN_OWNER_DECISION"


def test_approved_file_cannot_hold_open_or_unapproved_criteria():
    c = json.loads(DRAFT.read_text(encoding="utf-8"))
    c.update(status="APPROVED", approved_by="owner")
    errors = prereg.validate_criteria(c)
    assert any("A3_AVG_R_REPLACEMENT" in e for e in errors)
    assert any("G1_EXPECTANCY" in e for e in errors)


def test_unknown_metric_and_bad_op_are_rejected():
    c = json.loads(DRAFT.read_text(encoding="utf-8"))
    c["go"][0] = {**c["go"][0], "metric": "sharpe", "op": "~"}
    errors = prereg.validate_criteria(c)
    assert any("unknown metric" in e for e in errors) and any("op must be" in e for e in errors)


def test_seal_requires_future_start_unless_backdated(criteria):
    with pytest.raises(prereg.PreregError, match="^START_NOT_IN_FUTURE"):
        prereg.seal(criteria, START, now=START + timedelta(minutes=1))
    record = prereg.seal(criteria, START, now=START + timedelta(minutes=1), allow_backdate=True)
    assert record["backdated"] is True
    result = prereg.verify(record, criteria)
    assert result["ok"] and any(w.startswith("BACKDATED_SEAL") for w in result["warnings"])


def test_sealed_unchanged_file_verifies(criteria):
    record = prereg.seal(criteria, START, now=BEFORE)
    assert record["criteria_sha256"] == prereg.sha256_file(criteria) and record["backdated"] is False
    result = prereg.verify(record, criteria)
    assert result["ok"] and result["reasons"] == []
    assert any(w.startswith("CRITERIA_DRAFT_NOT_APPROVED") for w in result["warnings"])


def test_file_modified_after_start_is_refused(criteria):
    record = prereg.seal(criteria, START, now=BEFORE)
    c = json.loads(criteria.read_text(encoding="utf-8"))
    c["go"][0]["value"] = 0.10  # loosen a gate after the start
    criteria.write_text(json.dumps(c), encoding="utf-8")
    later = (START + timedelta(days=5)).timestamp()
    os.utime(criteria, (later, later))
    result = prereg.verify(record, criteria)
    assert not result["ok"] and result["reasons"] == ["CRITERIA_MODIFIED_AFTER_START"]


def test_file_changed_with_old_mtime_is_still_refused(criteria):
    record = prereg.seal(criteria, START, now=BEFORE)
    criteria.write_text(criteria.read_text(encoding="utf-8").replace("0.25", "0.2"), encoding="utf-8")
    stamp = BEFORE.timestamp()
    os.utime(criteria, (stamp, stamp))
    assert prereg.verify(record, criteria)["reasons"] == ["CRITERIA_HASH_MISMATCH"]


def test_touched_but_identical_file_only_warns(criteria):
    record = prereg.seal(criteria, START, now=BEFORE)
    later = (START + timedelta(days=1)).timestamp()
    os.utime(criteria, (later, later))
    result = prereg.verify(record, criteria)
    assert result["ok"] and any(w.startswith("CRITERIA_TOUCHED_AFTER_START") for w in result["warnings"])


def test_edited_seal_is_refused(criteria):
    record = prereg.seal(criteria, START, now=BEFORE)
    assert prereg.verify({**record, "start_utc": BEFORE.isoformat()}, criteria)["reasons"] == ["SEAL_DIGEST_MISMATCH"]
    assert prereg.verify(record, criteria.with_name("missing.json"))["reasons"] == ["CRITERIA_MISSING"]


def test_cli_seal_refuses_past_start(criteria, tmp_path, capsys):
    code = prereg.main(["seal", "--criteria", str(criteria), "--start", "2020-01-01T00:00:00Z",
                        "--out", str(tmp_path / "seal.json")])
    assert code == 2 and "START_NOT_IN_FUTURE" in capsys.readouterr().out
    assert not (tmp_path / "seal.json").exists()
