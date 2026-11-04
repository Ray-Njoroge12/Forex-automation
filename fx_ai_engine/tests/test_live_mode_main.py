"""`main.py --mode live` wiring, and the other modes staying exactly as they were.

Everything runs on temp bridge folders. The real MT5 terminal is never contacted.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import main
from core import live_approval, live_gate
from core.file_bridge_connection import FileBridgeConnection
from core.metrics import init_metrics
from core.observability import init_tracing
from tests.live_fixtures import (
    LOGIN,
    MICRO_LIVE_HASH,
    SERVER,
    FakeBridge,
    folder_state,
    make_real_bridge,
    make_scenario,
    risk_events,
)
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)
from tests.test_file_bridge_connection import build_bridge


@pytest.fixture(autouse=True)
def _clean_session():
    live_gate.reset_active_session()
    yield
    live_gate.reset_active_session()


@pytest.fixture
def runtime(tmp_path, monkeypatch, isolated_db):  # noqa: F811
    """Process environment for main.main() on a file bridge, without side effects."""
    # main() writes os.environ["FX_POLICY_MODE"] itself; setenv (not delenv) makes
    # monkeypatch restore the original state afterwards. An empty value counts as unset.
    monkeypatch.setenv("FX_POLICY_MODE", "")
    for name in (
        "USE_MT5_MOCK",
        "FX_ALLOW_NON_SRS_POLICY",
        "TELEGRAM_ENABLED",
        live_approval.APPROVAL_ENV,
        live_approval.CONFIRM_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MT5_CONNECTION", "file_bridge")
    monkeypatch.setattr(main, "start_health_api", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "run_all_migrations", lambda: None)
    monkeypatch.setattr(main, "load_runtime_env", lambda *_a, **_k: None)
    from application import engine_runtime, feedback_service

    monkeypatch.setattr(engine_runtime, "update_health_status", lambda **_: None)
    monkeypatch.setattr(feedback_service, "alert_trade_exit", lambda *a, **k: None)
    monkeypatch.setattr(feedback_service, "alert_trade_execution", lambda *a, **k: None)
    return monkeypatch


def _forbid_startup_gate(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("startup_gate must only run for --mode live")

    monkeypatch.setattr(live_gate, "startup_gate", boom)


def _live_scenario(tmp_path, monkeypatch, **kwargs):
    scenario = make_scenario(tmp_path, **kwargs)
    monkeypatch.setenv("BRIDGE_BASE_PATH", str(scenario.bridge_dir))
    monkeypatch.setenv(live_approval.APPROVAL_ENV, str(scenario.approval_path))
    monkeypatch.setenv(live_approval.CONFIRM_ENV, live_approval.expected_confirm_text(LOGIN))
    return scenario


def _recording_engine(monkeypatch):
    created: list = []
    real_engine = main.Engine

    def factory(*args, **kwargs):
        engine = real_engine(*args, **kwargs)
        created.append(engine)
        return engine

    monkeypatch.setattr(main, "Engine", factory)
    return created


def _refused(isolated_db, code):  # noqa: F811
    events = risk_events(isolated_db, "LIVE_STARTUP_REFUSED")
    assert len(events) == 1, events
    assert events[0]["reason"].startswith(code + " "), events[0]["reason"]


# ----------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------
def test_live_is_an_accepted_mode_and_smoke_is_still_the_default():
    assert main.parse_args(["--mode", "live"]).mode == "live"
    assert main.parse_args([]).mode == "smoke"
    assert main.parse_args(["--mode", "demo"]).mode == "demo"
    with pytest.raises(SystemExit):
        main.parse_args(["--mode", "paper"])


# ----------------------------------------------------------------------
# --mode live refusals (exit code 2, nothing written to the bridge)
# ----------------------------------------------------------------------
def test_live_on_a_demo_account_is_refused(tmp_path, runtime, isolated_db):  # noqa: F811
    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    runtime.setenv("BRIDGE_BASE_PATH", str(bridge_dir))
    before = folder_state(bridge_dir)
    assert main.main(["--mode", "live"]) == 2
    _refused(isolated_db, "LIVE_ACCOUNT_IS_DEMO")
    assert folder_state(bridge_dir) == before
    assert live_gate.active_session() is None


def test_live_under_the_mock_is_refused(tmp_path, runtime, isolated_db):  # noqa: F811
    runtime.setenv("USE_MT5_MOCK", "1")
    runtime.setenv("MT5_MOCK_BRIDGE_PATH", str(tmp_path / "mock_bridge"))
    assert main.main(["--mode", "live"]) == 2
    _refused(isolated_db, "LIVE_MOCK_FORBIDDEN")


def test_live_on_a_real_account_without_approval_is_refused(tmp_path, runtime, isolated_db):  # noqa: F811
    scenario = _live_scenario(tmp_path, runtime)
    scenario.approval_path.unlink()
    before = folder_state(scenario.bridge_dir)
    assert main.main(["--mode", "live"]) == 2
    _refused(isolated_db, "LIVE_APPROVAL_MISSING")
    assert folder_state(scenario.bridge_dir) == before
    assert risk_events(isolated_db, "NON_SRS_POLICY_APPROVAL") == []


def test_live_with_wrong_confirm_text_is_refused(tmp_path, runtime, isolated_db):  # noqa: F811
    scenario = _live_scenario(tmp_path, runtime)
    runtime.setenv(live_approval.CONFIRM_ENV, "yes")
    assert main.main(["--mode", "live"]) == 2
    _refused(isolated_db, "LIVE_CONFIRM_TEXT_WRONG")
    assert list((scenario.bridge_dir / "pending_signals").iterdir()) == []


def test_live_with_core_srs_policy_mode_is_refused(tmp_path, runtime, isolated_db):  # noqa: F811
    _live_scenario(tmp_path, runtime)
    assert main.main(["--mode", "live", "--policy-mode", "core_srs"]) == 2
    _refused(isolated_db, "LIVE_POLICY_MODE_MISMATCH")


def test_live_with_a_leaked_policy_env_is_refused(tmp_path, runtime, isolated_db):  # noqa: F811
    _live_scenario(tmp_path, runtime)
    runtime.setenv("FX_POLICY_MODE", "preserve_10")
    runtime.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
    runtime.setenv("MAX_SPREAD_PIPS", "9")
    assert main.main(["--mode", "live"]) == 2
    _refused(isolated_db, "LIVE_POLICY_MODE_MISMATCH")


# ----------------------------------------------------------------------
# Other modes are unchanged and never reach the gate
# ----------------------------------------------------------------------
def test_demo_on_a_real_account_is_still_refused_without_the_gate(tmp_path, runtime, isolated_db):  # noqa: F811
    scenario = _live_scenario(tmp_path, runtime)
    _forbid_startup_gate(runtime)
    before = folder_state(scenario.bridge_dir)
    assert main.main(["--mode", "demo"]) == 2
    assert folder_state(scenario.bridge_dir) == before
    assert risk_events(isolated_db, "LIVE_STARTUP_REFUSED") == []


def test_demo_on_a_demo_account_still_runs(tmp_path, runtime, isolated_db):  # noqa: F811
    bridge_dir = build_bridge(tmp_path / "bridge", datetime.now(timezone.utc), open_tickets=())
    runtime.setenv("BRIDGE_BASE_PATH", str(bridge_dir))
    _forbid_startup_gate(runtime)
    created = _recording_engine(runtime)
    assert main.main(["--mode", "demo", "--iterations", "1"]) == 0
    assert created[0].policy["MODE_ID"] == "core_srs"
    assert not hasattr(created[0], "_live_gate_session")
    assert list((bridge_dir / "pending_signals").iterdir()) == []


def test_smoke_under_the_mock_still_runs(tmp_path, runtime, isolated_db):  # noqa: F811
    runtime.setenv("USE_MT5_MOCK", "1")
    runtime.setenv("MT5_MOCK_BRIDGE_PATH", str(tmp_path / "mock_bridge"))
    runtime.delenv("MT5_CONNECTION", raising=False)
    _forbid_startup_gate(runtime)
    assert main.main(["--mode", "smoke"]) == 0


def test_runtime_approval_without_a_session_rejects_micro_live_on_a_real_account():
    policy = live_gate.get_policy_config("micro_live", env={})
    decision = main.evaluate_runtime_policy_approval(
        FakeBridge(LOGIN, SERVER, False), policy=policy, use_mock=False, env={"FX_ALLOW_NON_SRS_POLICY": "1"}
    )
    assert decision.approved is False
    assert decision.reason_code == "NON_SRS_POLICY_REQUIRES_MOCK_OR_DEMO"


# ----------------------------------------------------------------------
# A full valid live start
# ----------------------------------------------------------------------
def test_valid_live_start_runs_one_iteration(tmp_path, runtime, isolated_db):  # noqa: F811
    scenario = _live_scenario(tmp_path, runtime)
    created = _recording_engine(runtime)
    assert main.main(["--mode", "live", "--iterations", "1"]) == 0
    engine = created[0]
    assert engine.policy["MODE_ID"] == "micro_live"
    assert engine.run_mode == "live"
    assert engine.use_mock is False
    session = engine._live_gate_session
    assert session.policy_hash == MICRO_LIVE_HASH
    # The run is over, so the session no longer authorizes anything.
    assert session.closed is True
    assert live_gate.active_session() is None
    approvals = risk_events(isolated_db, "NON_SRS_POLICY_APPROVAL")
    assert approvals and approvals[0]["severity"] == "INFO"
    assert "MICRO_LIVE_LIVE_GATE_APPROVED" in approvals[0]["reason"]
    assert len(risk_events(isolated_db, "LIVE_STARTUP_APPROVED")) == 1
    assert risk_events(isolated_db, "LIVE_STARTUP_REFUSED") == []
    assert list((scenario.bridge_dir / "pending_signals").iterdir()) == []
    assert not (scenario.bridge_dir / "KILL_SWITCH").exists()


def test_valid_live_start_with_a_waiver_records_the_waiver(tmp_path, runtime, isolated_db):  # noqa: F811
    _live_scenario(tmp_path, runtime, waiver=True)
    assert main.main(["--mode", "live", "--iterations", "1"]) == 0
    assert len(risk_events(isolated_db, "LIVE_EVIDENCE_WAIVER")) == 1


def test_live_start_refuses_when_the_engine_cannot_attach(tmp_path, runtime, isolated_db):  # noqa: F811
    _live_scenario(tmp_path, runtime)
    real_engine = main.Engine

    def drifted(*args, **kwargs):
        engine = real_engine(*args, **kwargs)
        engine.policy = dict(engine.policy, BASE_RISK_PCT=0.02)
        return engine

    runtime.setattr(main, "Engine", drifted)
    assert main.main(["--mode", "live", "--iterations", "1"]) == 2
    _refused(isolated_db, "LIVE_POLICY_HASH_MISMATCH")


# ----------------------------------------------------------------------
# Real Engine loop: a halt stops the decision cycle
# ----------------------------------------------------------------------
def test_kill_switch_stops_the_decision_cycle_of_a_real_engine(tmp_path, runtime, isolated_db):  # noqa: F811
    scenario = _live_scenario(tmp_path, runtime)
    conn = FileBridgeConnection(scenario.bridge_dir)
    assert conn.connect(retry_count=1), conn.last_error
    assert conn.is_demo_account() is False
    scenario.bridge = conn
    session = scenario.gate(env=None)  # os.environ now carries the approval and confirm text
    assert session is not None
    engine = main.Engine(conn, init_tracing("test"), init_metrics(), use_mock=False, run_mode="live", policy=session.policy)
    assert session.attach(engine) is True

    cycles: list[int] = []
    engine._decision_cycle = lambda: cycles.append(1)
    engine._is_new_m15_candle = lambda: True
    (scenario.bridge_dir / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    before = folder_state(scenario.bridge_dir)

    engine.run(mode="live", iterations=1)

    assert cycles == []
    assert engine.account_status.is_trading_halted is True
    assert folder_state(scenario.bridge_dir) == before
    kill_events = risk_events(isolated_db, "LIVE_KILL_SWITCH")
    assert len(kill_events) == 1 and kill_events[0]["evidence_stream"] == "runtime_mt5_micro_live"
    assert kill_events[0]["account_scope"] == f"mt5:{SERVER}:{LOGIN}"
    decision = engine.hard_risk.validate(engine.account_status, 0.015)
    assert decision.approved is False and decision.reason_code == "RISK_HALTED"


def test_without_a_halt_the_decision_cycle_runs(tmp_path, runtime, isolated_db):  # noqa: F811
    scenario = _live_scenario(tmp_path, runtime)
    conn = FileBridgeConnection(scenario.bridge_dir)
    assert conn.connect(retry_count=1)
    scenario.bridge = conn
    session = scenario.gate(env=None)
    engine = main.Engine(conn, init_tracing("test"), init_metrics(), use_mock=False, run_mode="live", policy=session.policy)
    assert session.attach(engine) is True
    cycles: list[int] = []
    engine._decision_cycle = lambda: cycles.append(1)
    engine._is_new_m15_candle = lambda: True
    engine.run(mode="live", iterations=1)
    assert cycles == [1]
    assert engine.account_status.is_trading_halted is False


# ----------------------------------------------------------------------
# A finished or abandoned live session never authorizes a later invocation
# ----------------------------------------------------------------------
def _assert_smoke_micro_live_blocked(isolated_db):  # noqa: F811
    argv = ["--mode", "smoke", "--policy-mode", "micro_live", "--iterations", "1"]
    assert main.main(argv) == 4
    approvals = risk_events(isolated_db, "NON_SRS_POLICY_APPROVAL")
    assert approvals and approvals[-1]["severity"] == "BLOCK"
    assert "MICRO_LIVE_LIVE_GATE_APPROVED" not in approvals[-1]["reason"]
    assert "NON_SRS_POLICY_EXPLICIT_APPROVAL_REQUIRED" in approvals[-1]["reason"]


def test_smoke_micro_live_after_a_finished_live_run_is_blocked(tmp_path, runtime, isolated_db):  # noqa: F811
    _live_scenario(tmp_path, runtime)
    assert main.main(["--mode", "live", "--iterations", "1"]) == 0
    assert live_gate.active_session() is None
    _assert_smoke_micro_live_blocked(isolated_db)


def test_smoke_micro_live_after_an_abandoned_live_session_is_blocked(tmp_path, runtime, isolated_db):  # noqa: F811
    # The gate passed but the run never reached engine.run (and its bridge never shut down).
    scenario = _live_scenario(tmp_path, runtime)
    assert scenario.gate(env=None) is not None
    assert live_gate.active_session() is not None
    _assert_smoke_micro_live_blocked(isolated_db)


def test_smoke_micro_live_after_the_approval_expired_is_blocked(tmp_path, runtime, isolated_db):  # noqa: F811
    scenario = _live_scenario(tmp_path, runtime)
    session = scenario.gate(env=None)
    session._clock = lambda: session.approval.expires_at_utc
    _assert_smoke_micro_live_blocked(isolated_db)


def test_live_run_that_fails_to_attach_leaves_no_session_behind(tmp_path, runtime, isolated_db):  # noqa: F811
    _live_scenario(tmp_path, runtime)
    real_engine = main.Engine

    def drifted(*args, **kwargs):
        engine = real_engine(*args, **kwargs)
        engine.policy = dict(engine.policy, BASE_RISK_PCT=0.02)
        return engine

    runtime.setattr(main, "Engine", drifted)
    assert main.main(["--mode", "live", "--iterations", "1"]) == 2
    assert live_gate.active_session() is None
    runtime.setattr(main, "Engine", real_engine)
    _assert_smoke_micro_live_blocked(isolated_db)
