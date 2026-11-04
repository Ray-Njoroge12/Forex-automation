"""Startup gate for --mode live: one test per refusal code, plus the valid start."""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from core import live_approval, live_gate
from tests.live_fixtures import (
    DROP,
    EA_VERSION,
    LOGIN,
    MICRO_LIVE_HASH,
    SERVER,
    FakeBridge,
    file_sha256,
    folder_state,
    make_scenario,
    risk_events,
    write_live_guard,
    write_status,
    write_verdict,
)
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _clean_session():
    live_gate.reset_active_session()
    yield
    live_gate.reset_active_session()


@pytest.fixture
def scenario(tmp_path, isolated_db):  # noqa: F811
    return make_scenario(tmp_path)


def _assert_refused(scenario, isolated_db, code, **gate_kwargs):  # noqa: F811
    before = folder_state(scenario.bridge_dir)
    session = scenario.gate(**gate_kwargs)
    assert session is None
    assert live_gate.active_session() is None
    events = risk_events(isolated_db, "LIVE_STARTUP_REFUSED")
    assert len(events) == 1, events
    assert events[0]["severity"] == "BLOCK"
    assert events[0]["reason"].startswith(code + " "), events[0]["reason"]
    assert events[0]["evidence_stream"] == "runtime_mt5_micro_live"
    assert folder_state(scenario.bridge_dir) == before
    assert list((scenario.bridge_dir / "pending_signals").iterdir()) == []
    assert not (scenario.bridge_dir / "KILL_SWITCH").exists()


# ----------------------------------------------------------------------
# Valid start
# ----------------------------------------------------------------------
def test_valid_scenario_starts_a_session(scenario, isolated_db):  # noqa: F811
    before = folder_state(scenario.bridge_dir)
    session = scenario.gate()
    assert session is not None
    assert live_gate.active_session() is session
    assert session.login == LOGIN and session.server == SERVER
    assert session.policy_hash == MICRO_LIVE_HASH
    assert session.waiver_active is False
    assert session.evidence_route == "verdict"
    assert risk_events(isolated_db, "LIVE_STARTUP_REFUSED") == []
    approved = risk_events(isolated_db, "LIVE_STARTUP_APPROVED")
    assert len(approved) == 1 and approved[0]["severity"] == "INFO"
    assert MICRO_LIVE_HASH in approved[0]["reason"]
    assert risk_events(isolated_db, "LIVE_EVIDENCE_WAIVER") == []
    assert folder_state(scenario.bridge_dir) == before


def test_a_refused_start_clears_a_previous_session(scenario, isolated_db):  # noqa: F811
    assert scenario.gate() is not None
    scenario.approval_path.unlink()
    assert scenario.gate() is None
    assert live_gate.active_session() is None


def test_gate_reports_the_running_strategy_id(scenario, isolated_db):  # noqa: F811
    _assert_refused(scenario, isolated_db, "LIVE_STRATEGY_MISMATCH", running_strategy_id="other_strategy_v2")


def test_running_strategy_id_trend_pullback_is_accepted(scenario, isolated_db):  # noqa: F811
    assert scenario.gate(running_strategy_id="trend_pullback_v1") is not None


# ----------------------------------------------------------------------
# Waiver
# ----------------------------------------------------------------------
def test_waiver_start_is_loud(tmp_path, isolated_db, caplog):  # noqa: F811
    scenario = make_scenario(tmp_path, waiver=True)
    with caplog.at_level(logging.CRITICAL, logger="fx_ai_engine.live_gate"):
        session = scenario.gate()
    assert session is not None and session.waiver_active
    assert any(r.levelno == logging.CRITICAL and "WAIVER" in r.getMessage() for r in caplog.records)
    events = risk_events(isolated_db, "LIVE_EVIDENCE_WAIVER")
    assert len(events) == 1 and events[0]["severity"] == "CRITICAL"
    assert "no micro_live replay yet" in events[0]["reason"]


def test_waiver_overrides_a_failing_verdict(tmp_path, isolated_db):  # noqa: F811
    scenario = make_scenario(tmp_path, waiver=True)
    scenario.rewrite_approval(
        evidence={"verdict_path": str(tmp_path / "gone.json"), "sha256": "0" * 64},
        evidence_waiver={"reason": "accepted", "accepted_by": "Owner"},
    )
    session = scenario.gate()
    assert session is not None and session.waiver_active


# ----------------------------------------------------------------------
# Refusals, in the order of the gate
# ----------------------------------------------------------------------
def test_policy_mode_argument_other_than_micro_live(scenario, isolated_db):  # noqa: F811
    _assert_refused(scenario, isolated_db, "LIVE_POLICY_MODE_MISMATCH", policy_mode_arg="core_srs")


def test_policy_mode_env_other_than_micro_live(scenario, isolated_db):  # noqa: F811
    scenario.env["FX_POLICY_MODE"] = "core_srs"
    _assert_refused(scenario, isolated_db, "LIVE_POLICY_MODE_MISMATCH")


def test_micro_live_argument_and_env_are_accepted(scenario, isolated_db):  # noqa: F811
    scenario.env["FX_POLICY_MODE"] = "micro_live"
    assert scenario.gate(policy_mode_arg="micro_live") is not None


def test_env_quirk_preserve_10_leak_cannot_move_the_policy(scenario, isolated_db):  # noqa: F811
    scenario.env.update({"FX_POLICY_MODE": "preserve_10", "FX_ALLOW_NON_SRS_POLICY": "1", "MAX_SPREAD_PIPS": "9"})
    _assert_refused(scenario, isolated_db, "LIVE_POLICY_MODE_MISMATCH")


def test_override_env_with_micro_live_mode_keeps_the_approved_policy(scenario, isolated_db):  # noqa: F811
    scenario.env.update(
        {"FX_POLICY_MODE": "micro_live", "FX_ALLOW_NON_SRS_POLICY": "1", "MAX_SPREAD_PIPS": "9", "FIXED_RISK_USD": "5"}
    )
    session = scenario.gate()
    assert session is not None
    assert session.policy["MAX_SPREAD_PIPS"] == 2.0
    assert session.policy["FIXED_RISK_USD"] is None
    assert session.policy_hash == MICRO_LIVE_HASH


def test_mock_is_forbidden(scenario, isolated_db):  # noqa: F811
    _assert_refused(scenario, isolated_db, "LIVE_MOCK_FORBIDDEN", use_mock=True)


def test_mock_env_flag_is_forbidden(scenario, isolated_db):  # noqa: F811
    scenario.env["USE_MT5_MOCK"] = "1"
    _assert_refused(scenario, isolated_db, "LIVE_MOCK_FORBIDDEN")


def test_demo_account_is_refused(scenario, isolated_db):  # noqa: F811
    scenario.bridge = FakeBridge(LOGIN, SERVER, True, scenario.bridge_dir)
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_IS_DEMO")


def test_unverified_account_is_refused(scenario, isolated_db):  # noqa: F811
    scenario.bridge = FakeBridge(LOGIN, SERVER, None, scenario.bridge_dir)
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_UNVERIFIED")


def test_account_check_that_raises_is_refused(scenario, isolated_db):  # noqa: F811
    scenario.bridge = FakeBridge(LOGIN, SERVER, RuntimeError("terminal gone"), scenario.bridge_dir)
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_UNVERIFIED")


def test_missing_approval_file(scenario, isolated_db):  # noqa: F811
    scenario.approval_path.unlink()
    _assert_refused(scenario, isolated_db, "LIVE_APPROVAL_MISSING")


def test_missing_approval_env_uses_the_default_path_and_refuses(scenario, isolated_db, monkeypatch, tmp_path):  # noqa: F811
    scenario.env.pop(live_approval.APPROVAL_ENV)
    monkeypatch.setattr(live_gate, "DEFAULT_APPROVAL_PATH", tmp_path / "no_such_default.json")
    _assert_refused(scenario, isolated_db, "LIVE_APPROVAL_MISSING")


def test_malformed_approval_file(scenario, isolated_db):  # noqa: F811
    scenario.approval_path.write_text("{broken", encoding="utf-8")
    _assert_refused(scenario, isolated_db, "LIVE_APPROVAL_MALFORMED")


def test_expired_approval_file(scenario, isolated_db):  # noqa: F811
    scenario.rewrite_approval(
        approved_at_utc=(scenario.now - timedelta(days=8)).isoformat(),
        expires_at_utc=(scenario.now - timedelta(hours=1)).isoformat(),
    )
    _assert_refused(scenario, isolated_db, "LIVE_APPROVAL_EXPIRED")


def test_approval_login_differs(scenario, isolated_db):  # noqa: F811
    scenario.rewrite_approval(account_login=LOGIN + 1)
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_MISMATCH")


def test_approval_server_differs(scenario, isolated_db):  # noqa: F811
    scenario.rewrite_approval(account_server="Other-Server")
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_MISMATCH")


def test_connected_bridge_login_differs(scenario, isolated_db):  # noqa: F811
    scenario.bridge = FakeBridge(LOGIN + 1, SERVER, False, scenario.bridge_dir)
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_MISMATCH")


def test_ea_status_login_differs(scenario, isolated_db):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now, login=LOGIN + 1)
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_MISMATCH")


def test_ea_status_server_differs(scenario, isolated_db):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now, server="Other-Server")
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_MISMATCH")


def test_policy_hash_differs(scenario, isolated_db):  # noqa: F811
    scenario.rewrite_approval(policy_hash="f" * 64)
    _assert_refused(scenario, isolated_db, "LIVE_POLICY_HASH_MISMATCH")


def test_core_srs_hash_is_not_accepted_for_live(scenario, isolated_db):  # noqa: F811
    scenario.rewrite_approval(policy_hash="f00f1f9c3e9120971378f3c1eb95d28f09d1d5e2ee97035a7f4c00a998535dc8")
    _assert_refused(scenario, isolated_db, "LIVE_POLICY_HASH_MISMATCH")


def test_strategy_differs(scenario, isolated_db):  # noqa: F811
    scenario.rewrite_approval(strategy_id="other_strategy_v2")
    _assert_refused(scenario, isolated_db, "LIVE_STRATEGY_MISMATCH")


def test_confirm_text_wrong(scenario, isolated_db):  # noqa: F811
    scenario.env[live_approval.CONFIRM_ENV] = "I-ACCEPT-LIVE-RISK-1"
    _assert_refused(scenario, isolated_db, "LIVE_CONFIRM_TEXT_WRONG")


def test_confirm_text_missing(scenario, isolated_db):  # noqa: F811
    scenario.env.pop(live_approval.CONFIRM_ENV)
    _assert_refused(scenario, isolated_db, "LIVE_CONFIRM_TEXT_WRONG")


def test_evidence_missing_without_waiver(scenario, isolated_db):  # noqa: F811
    scenario.rewrite_approval(evidence=DROP)
    _assert_refused(scenario, isolated_db, "LIVE_EVIDENCE_MISSING")


def test_evidence_file_missing(scenario, isolated_db):  # noqa: F811
    scenario.verdict_path.unlink()
    _assert_refused(scenario, isolated_db, "LIVE_EVIDENCE_FILE_MISSING")


def test_evidence_sha_mismatch(scenario, isolated_db):  # noqa: F811
    write_verdict(scenario.verdict_path, note="edited after approval")
    _assert_refused(scenario, isolated_db, "LIVE_EVIDENCE_HASH_MISMATCH")


def test_evidence_verdict_not_pass(scenario, isolated_db):  # noqa: F811
    write_verdict(scenario.verdict_path, verdict="FAIL")
    scenario.rewrite_approval()
    _assert_refused(scenario, isolated_db, "LIVE_EVIDENCE_NOT_PASS")


def test_evidence_status_not_eligible(scenario, isolated_db):  # noqa: F811
    write_verdict(scenario.verdict_path, evidence_status="NOT_EVIDENCE")
    scenario.rewrite_approval()
    _assert_refused(scenario, isolated_db, "LIVE_EVIDENCE_NOT_ELIGIBLE")


def test_evidence_policy_hash_differs(scenario, isolated_db):  # noqa: F811
    write_verdict(scenario.verdict_path, policy_hash="f00f1f9c3e9120971378f3c1eb95d28f09d1d5e2ee97035a7f4c00a998535dc8")
    scenario.rewrite_approval()
    _assert_refused(scenario, isolated_db, "LIVE_EVIDENCE_POLICY_MISMATCH")


def test_ea_status_missing(scenario, isolated_db):  # noqa: F811
    (scenario.bridge_dir / "market" / "status.json").unlink()
    _assert_refused(scenario, isolated_db, "LIVE_EA_STATUS_UNAVAILABLE")


def test_ea_status_malformed(scenario, isolated_db):  # noqa: F811
    (scenario.bridge_dir / "market" / "status.json").write_text("{broken", encoding="utf-8")
    _assert_refused(scenario, isolated_db, "LIVE_EA_STATUS_UNAVAILABLE")


def test_ea_status_older_than_30_seconds(scenario, isolated_db):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now - timedelta(seconds=40))
    _assert_refused(scenario, isolated_db, "LIVE_EA_STATUS_UNAVAILABLE")


def test_ea_status_wrong_schema(scenario, isolated_db):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now, schema_version=2)
    _assert_refused(scenario, isolated_db, "LIVE_EA_STATUS_UNAVAILABLE")


def test_ea_status_schema_true_is_not_one(scenario, isolated_db):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now, schema_version=True)
    _assert_refused(scenario, isolated_db, "LIVE_EA_STATUS_UNAVAILABLE")


def test_ea_status_reports_demo(scenario, isolated_db):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now, account_is_demo=True)
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_IS_DEMO")


def test_ea_status_without_demo_flag(scenario, isolated_db):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now, account_is_demo=DROP)
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_UNVERIFIED")


@pytest.mark.parametrize("version", ["2026.11.04-1", "2026.11.03-9", "test", ""])
def test_ea_version_too_old_or_unparsable(scenario, isolated_db, version):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now, ea_version=version)
    _assert_refused(scenario, isolated_db, "LIVE_EA_VERSION_TOO_OLD")


def test_ea_version_missing(scenario, isolated_db):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now, ea_version=DROP)
    _assert_refused(scenario, isolated_db, "LIVE_EA_VERSION_TOO_OLD")


def test_ea_version_newer_is_accepted(scenario, isolated_db):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now, ea_version="2026.12.01-1")
    assert scenario.gate() is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"account_real": False},
        {"allow_live": False},
        {"kill_switch": True},
        {"max_lot": 0.02},
        {"max_orders_per_day": 5},
        {"max_lot": DROP},
        {"max_orders_per_day": DROP},
        {"max_lot": "0.01"},
    ],
)
def test_live_guard_mismatch(scenario, isolated_db, changes):  # noqa: F811
    write_live_guard(scenario.bridge_dir, scenario.now, **changes)
    _assert_refused(scenario, isolated_db, "LIVE_GUARD_MISMATCH")


def test_live_guard_block_missing(scenario, isolated_db):  # noqa: F811
    write_status(scenario.bridge_dir, scenario.now, live_guard=DROP)
    _assert_refused(scenario, isolated_db, "LIVE_GUARD_MISMATCH")


def test_stricter_live_guard_is_accepted(scenario, isolated_db):  # noqa: F811
    write_live_guard(scenario.bridge_dir, scenario.now, max_lot=0.005, max_orders_per_day=2)
    assert scenario.gate() is not None


# ----------------------------------------------------------------------
# Ordering, unexpected failures and the covers() predicate
# ----------------------------------------------------------------------
def test_earlier_check_wins_when_two_things_are_wrong(scenario, isolated_db):  # noqa: F811
    scenario.bridge = FakeBridge(LOGIN, SERVER, True, scenario.bridge_dir)
    scenario.approval_path.unlink()
    _assert_refused(scenario, isolated_db, "LIVE_ACCOUNT_IS_DEMO")


def test_unexpected_exception_is_a_refusal(scenario, isolated_db, monkeypatch):  # noqa: F811
    def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(live_approval, "load_approval", boom)
    _assert_refused(scenario, isolated_db, "LIVE_GATE_INTERNAL_ERROR")


def test_refusal_is_returned_even_if_the_event_cannot_be_stored(scenario, monkeypatch):  # noqa: F811
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(live_gate.db, "insert_risk_event", boom)
    scenario.approval_path.unlink()
    assert scenario.gate() is None


def test_live_session_covers_only_the_approved_policy_and_login(scenario, isolated_db):  # noqa: F811
    from config_microcapital import get_policy_config

    assert live_gate.live_session_covers(get_policy_config("micro_live", env={}), scenario.bridge) is False
    session = scenario.gate()
    policy = dict(session.policy)
    assert live_gate.live_session_covers(policy, scenario.bridge) is True
    assert live_gate.live_session_covers(get_policy_config("core_srs", env={}), scenario.bridge) is False
    assert live_gate.live_session_covers(get_policy_config("preserve_10", env={}), scenario.bridge) is False
    changed = dict(policy, BASE_RISK_PCT=0.02)
    assert live_gate.live_session_covers(changed, scenario.bridge) is False
    relabelled = dict(policy, MODE_ID="core_srs")
    assert live_gate.live_session_covers(relabelled, scenario.bridge) is False
    other_login = FakeBridge(LOGIN + 1, SERVER, False, scenario.bridge_dir)
    assert live_gate.live_session_covers(policy, other_login) is False
    assert live_gate.live_session_covers(policy, object()) is False
    live_gate.reset_active_session()
    assert live_gate.live_session_covers(policy, scenario.bridge) is False


def session_sha(scenario):
    return live_gate.active_session().approval_sha256


def test_startup_never_writes_even_on_success(scenario, isolated_db):  # noqa: F811
    before = folder_state(scenario.bridge_dir)
    assert scenario.gate() is not None
    assert folder_state(scenario.bridge_dir) == before
    assert file_sha256(scenario.approval_path) == session_sha(scenario)
