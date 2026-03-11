from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

import main as main_mod
from config_microcapital import get_policy_config
from core.mt5_bridge import (
    PRESERVE_10_REQUIRED_SYMBOLS,
    Preserve10AccountFacts,
    Preserve10ApprovalFacts,
    Preserve10SymbolFacts,
)


NOW_UTC = datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)


class _FakeBridge:
    def __init__(self, facts: Preserve10ApprovalFacts):
        self.facts = facts
        self.calls = 0

    def get_preserve_10_approval_facts(self, *, now=None):
        self.calls += 1
        return self.facts

    def fetch_ohlc_data(self, *_args, **_kwargs):
        return pd.DataFrame()

    def get_live_spread(self, _symbol: str) -> float:
        return 0.0001


class _FakeTracer:
    def start_as_current_span(self, _name: str):
        raise AssertionError("Engine.run() should not be exercised in startup-constructor tests")


class _FakeMetrics:
    def inc(self, _name: str) -> None:
        return None

    def set_gauge(self, _name: str, _value: float) -> None:
        return None


class _FakeRanker:
    def load(self) -> bool:
        return False

    def predict_proba(self, _features: dict[str, float]) -> float:
        return 0.5


def _prepare_engine_init(monkeypatch, tmp_path) -> None:
    for folder in ("pending_signals", "feedback", "exits", "active_locks"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_mod, "get_mt5_bridge_path", lambda: tmp_path)
    monkeypatch.setattr(main_mod, "load_calendar", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main_mod, "load_rate_differentials", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main_mod, "SentimentAgent", lambda: object())
    monkeypatch.setattr(main_mod, "SignalRanker", _FakeRanker)


def _approved_symbol_facts() -> dict[str, Preserve10SymbolFacts]:
    facts: dict[str, Preserve10SymbolFacts] = {}
    for symbol in PRESERVE_10_REQUIRED_SYMBOLS:
        is_jpy = symbol.endswith("JPY")
        facts[symbol] = Preserve10SymbolFacts(
            symbol=symbol,
            trade_mode=1,
            tradable=True,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=10.0,
            contract_size=100000.0,
            tick_value=1.0,
            tick_size=0.001 if is_jpy else 0.00001,
            point=0.001 if is_jpy else 0.00001,
            digits=3 if is_jpy else 5,
            stops_level=0,
            freeze_level=0,
            spread_price=0.00008,
            spread_pips=0.8,
            min_lot_margin=25.0,
            quote_time_utc=NOW_UTC.isoformat(),
            quote_age_seconds=15,
        )
    return facts


def _approval_facts(*, trade_allowed: bool = True) -> Preserve10ApprovalFacts:
    return Preserve10ApprovalFacts(
        can_assess=True,
        reason_code="APPROVAL_FACTS_READY",
        details="ready",
        fetched_at_utc=NOW_UTC.isoformat(),
        account=Preserve10AccountFacts(
            currency="USC",
            denomination="usd_cent",
            unit_scale=0.01,
            reported_balance=1000.0,
            reported_equity=1000.0,
            normalized_balance_usd=10.0,
            normalized_equity_usd=10.0,
            leverage=500,
            trade_allowed=trade_allowed,
        ),
        symbols=_approved_symbol_facts(),
    )


def test_preserve_10_startup_gate_approves_when_facts_and_cost_evidence_are_ready(monkeypatch) -> None:
    monkeypatch.setenv(main_mod.PRESERVE_10_COMMISSION_PER_LOT_ENV, "0")

    decision = main_mod.evaluate_preserve_10_startup_approval(
        _FakeBridge(_approval_facts()),
        policy=get_policy_config(mode_id="preserve_10"),
        env={main_mod.PRESERVE_10_COMMISSION_PER_LOT_ENV: "0"},
        now=NOW_UTC,
    )

    assert decision.approved is True
    assert decision.reason_code == "PRESERVE_10_STARTUP_APPROVED"


def test_preserve_10_startup_gate_rejects_denied_account(monkeypatch) -> None:
    monkeypatch.setenv(main_mod.PRESERVE_10_COMMISSION_PER_LOT_ENV, "0")

    decision = main_mod.evaluate_preserve_10_startup_approval(
        _FakeBridge(_approval_facts(trade_allowed=False)),
        policy=get_policy_config(mode_id="preserve_10"),
        env={main_mod.PRESERVE_10_COMMISSION_PER_LOT_ENV: "0"},
        now=NOW_UTC,
    )

    assert decision.approved is False
    assert decision.reason_code == "PRESERVE_10_ACCOUNT_TRADE_DISABLED"


def test_preserve_10_startup_gate_rejects_stale_approval_snapshot() -> None:
    stale = Preserve10ApprovalFacts(
        can_assess=False,
        reason_code="APPROVAL_SYMBOL_TICK_STALE",
        details="quote too old for symbol=GBPUSD age_seconds=600 max_age_seconds=120",
        fetched_at_utc=NOW_UTC.isoformat(),
    )

    decision = main_mod.evaluate_preserve_10_startup_approval(
        _FakeBridge(stale),
        policy=get_policy_config(mode_id="preserve_10"),
        now=NOW_UTC,
    )

    assert decision.approved is False
    assert decision.reason_code == "APPROVAL_SYMBOL_TICK_STALE"
    assert "blocked before engine start" in decision.details.lower()
    assert "source_detail=quote too old" in decision.details
    assert "evidence=Preserve-$10 doctrine" in decision.details


def test_preserve_10_startup_gate_rejects_missing_approval_snapshot() -> None:
    unavailable = Preserve10ApprovalFacts(
        can_assess=False,
        reason_code="APPROVAL_ACCOUNT_INFO_MISSING",
        details="account_info() returned None",
        fetched_at_utc=NOW_UTC.isoformat(),
    )

    decision = main_mod.evaluate_preserve_10_startup_approval(
        _FakeBridge(unavailable),
        policy=get_policy_config(mode_id="preserve_10"),
        now=NOW_UTC,
    )

    assert decision.approved is False
    assert decision.reason_code == "APPROVAL_ACCOUNT_INFO_MISSING"
    assert "blocked before engine start" in decision.details.lower()
    assert "account_info() returned None" in decision.details


def test_preserve_10_startup_gate_rejects_when_cost_evidence_is_unavailable() -> None:
    decision = main_mod.evaluate_preserve_10_startup_approval(
        _FakeBridge(_approval_facts()),
        policy=get_policy_config(mode_id="preserve_10"),
        env={},
        now=NOW_UTC,
    )

    assert decision.approved is False
    assert decision.reason_code == "PRESERVE_10_COST_EVIDENCE_UNAVAILABLE"
    assert main_mod.PRESERVE_10_COMMISSION_PER_LOT_ENV in decision.details
    assert "blocked before engine start" in decision.details.lower()


def test_preserve_10_startup_gate_rejects_invalid_cost_evidence() -> None:
    decision = main_mod.evaluate_preserve_10_startup_approval(
        _FakeBridge(_approval_facts()),
        policy=get_policy_config(mode_id="preserve_10"),
        env={main_mod.PRESERVE_10_COMMISSION_PER_LOT_ENV: "invalid"},
        now=NOW_UTC,
    )

    assert decision.approved is False
    assert decision.reason_code == "PRESERVE_10_COST_EVIDENCE_INVALID"
    assert "commission evidence is invalid" in decision.details


def test_core_srs_bypasses_preserve_10_startup_gate() -> None:
    bridge = _FakeBridge(_approval_facts())

    decision = main_mod.evaluate_preserve_10_startup_approval(
        bridge,
        policy=get_policy_config(mode_id="core_srs"),
        env={},
        now=NOW_UTC,
    )

    assert decision.approved is True
    assert decision.reason_code == "PRESERVE_10_STARTUP_GATE_BYPASS"
    assert bridge.calls == 0


def test_engine_constructor_blocks_preserve_10_without_startup_approval(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
    monkeypatch.delenv(main_mod.PRESERVE_10_COMMISSION_PER_LOT_ENV, raising=False)
    _prepare_engine_init(monkeypatch, tmp_path)

    bridge = _FakeBridge(_approval_facts())

    try:
        main_mod.Engine(
            bridge=bridge,
            tracer=_FakeTracer(),
            metrics=_FakeMetrics(),
            use_mock=True,
            run_mode="smoke",
        )
        raise AssertionError("Engine constructor should reject preserve_10 without startup approval")
    except RuntimeError as exc:
        assert "Preserve-$10 startup approval refused" in str(exc)
        assert "PRESERVE_10_COST_EVIDENCE_UNAVAILABLE" in str(exc)


def test_engine_constructor_allows_preserve_10_with_startup_approval(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
    monkeypatch.setenv(main_mod.PRESERVE_10_COMMISSION_PER_LOT_ENV, "0")
    _prepare_engine_init(monkeypatch, tmp_path)

    bridge = _FakeBridge(_approval_facts())
    engine = main_mod.Engine(
        bridge=bridge,
        tracer=_FakeTracer(),
        metrics=_FakeMetrics(),
        use_mock=True,
        run_mode="smoke",
    )

    assert engine.policy["MODE_ID"] == "preserve_10"
    assert engine.portfolio_manager.mode_id == "preserve_10"
    assert engine.hard_risk.mode_id == "preserve_10"
    assert bridge.calls == 1


def test_main_preserve_10_startup_refusal_shuts_down_bridge_before_engine_init(monkeypatch) -> None:
    risk_events = []
    created: dict[str, object] = {}

    class _RefusingBridge:
        def __init__(self, *_args, **_kwargs):
            self.events: list[str] = []
            created["bridge"] = self

        def connect(self) -> bool:
            self.events.append("connect")
            return True

        def get_preserve_10_approval_facts(self, *, now=None):
            self.events.append("approval_facts")
            return Preserve10ApprovalFacts(
                can_assess=False,
                reason_code="APPROVAL_ACCOUNT_INFO_MISSING",
                details="account_info() returned None",
                fetched_at_utc=NOW_UTC.isoformat(),
            )

        def shutdown(self) -> None:
            self.events.append("shutdown")

    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    monkeypatch.delenv(main_mod.PRESERVE_10_COMMISSION_PER_LOT_ENV, raising=False)
    monkeypatch.setattr(main_mod, "initialize_schema", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_phase8_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_risk_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_ml_feature_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_restart_state_columns", lambda: None)
    monkeypatch.setattr(main_mod, "init_tracing", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_mod, "init_metrics", lambda: object())
    monkeypatch.setattr(main_mod, "MT5Connection", _RefusingBridge)
    monkeypatch.setattr(main_mod, "insert_risk_event", lambda *args: risk_events.append(args))

    def _unexpected_engine(*_args, **_kwargs):
        raise AssertionError("Engine should not be constructed when startup gate refuses")

    monkeypatch.setattr(main_mod, "Engine", _unexpected_engine)

    exit_code = main_mod.main([])

    assert exit_code == 3
    assert created["bridge"].events == ["connect", "approval_facts", "shutdown"]
    assert risk_events[-1][0] == "PRESERVE_10_STARTUP_APPROVAL"
    assert risk_events[-1][1] == "BLOCK"
    assert "blocked before engine start" in risk_events[-1][2].lower()
    assert "evidence=Preserve-$10 doctrine" in risk_events[-1][2]


def test_main_cli_policy_mode_overrides_legacy_micro_capital_env(monkeypatch) -> None:
    created: dict[str, object] = {}

    class _Bridge:
        def __init__(self, login, password, server):
            self.login = login
            self.password = password
            self.server = server

        def connect(self) -> bool:
            return True

        def shutdown(self) -> None:
            return None

    class _Engine:
        def __init__(self, bridge, tracer, metrics, *, use_mock, run_mode, policy):
            created["policy"] = policy
            created["use_mock"] = use_mock
            created["run_mode"] = run_mode

        def run(self, mode: str, iterations: int = 0) -> None:
            created["run"] = (mode, iterations)

    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setenv("MICRO_CAPITAL_MODE", "1")
    monkeypatch.delenv("FX_POLICY_MODE", raising=False)
    monkeypatch.setattr(main_mod, "load_runtime_env", lambda: None)
    monkeypatch.setattr(main_mod, "initialize_schema", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_phase8_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_risk_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_decision_funnel_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_ml_feature_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_restart_state_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_evidence_partition_columns", lambda: None)
    monkeypatch.setattr(main_mod, "init_tracing", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_mod, "init_metrics", lambda: object())
    monkeypatch.setattr(main_mod, "MT5Connection", _Bridge)
    monkeypatch.setattr(main_mod, "Engine", _Engine)

    exit_code = main_mod.main(["--mode", "smoke", "--policy-mode", "core_srs"])

    assert exit_code == 0
    assert created["use_mock"] is True
    assert created["run_mode"] == "smoke"
    assert created["run"] == ("smoke", 0)
    assert created["policy"]["MODE_ID"] == "core_srs"
    assert main_mod.os.environ["FX_POLICY_MODE"] == "core_srs"


def test_main_blocks_non_srs_policy_without_explicit_approval(monkeypatch) -> None:
    risk_events = []

    class _Bridge:
        def __init__(self, login, password, server):
            self.login = login
            self.password = password
            self.server = server

        def connect(self) -> bool:
            return True

        def shutdown(self) -> None:
            return None

    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.delenv("FX_ALLOW_NON_SRS_POLICY", raising=False)
    monkeypatch.setattr(main_mod, "load_runtime_env", lambda: None)
    monkeypatch.setattr(main_mod, "initialize_schema", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_phase8_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_risk_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_decision_funnel_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_ml_feature_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_restart_state_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_evidence_partition_columns", lambda: None)
    monkeypatch.setattr(main_mod, "init_tracing", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_mod, "init_metrics", lambda: object())
    monkeypatch.setattr(main_mod, "MT5Connection", _Bridge)
    monkeypatch.setattr(main_mod, "insert_risk_event", lambda *args: risk_events.append(args))
    monkeypatch.setattr(main_mod, "Engine", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Engine should not start")))

    exit_code = main_mod.main(["--mode", "smoke", "--policy-mode", "legacy_micro_capital"])

    assert exit_code == 4
    assert risk_events[-1][0] == "NON_SRS_POLICY_APPROVAL"
    assert risk_events[-1][1] == "BLOCK"
    assert "non_srs_policy_explicit_approval_required" in risk_events[-1][2].lower()


def test_main_allows_non_srs_policy_in_mock_mode_with_explicit_approval(monkeypatch) -> None:
    created: dict[str, object] = {}

    class _Bridge:
        def __init__(self, login, password, server):
            self.login = login
            self.password = password
            self.server = server

        def connect(self) -> bool:
            return True

        def shutdown(self) -> None:
            return None

    class _Engine:
        def __init__(self, bridge, tracer, metrics, *, use_mock, run_mode, policy):
            created["policy"] = policy
            created["use_mock"] = use_mock
            created["run_mode"] = run_mode

        def run(self, mode: str, iterations: int = 0) -> None:
            created["run"] = (mode, iterations)

    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
    monkeypatch.setattr(main_mod, "load_runtime_env", lambda: None)
    monkeypatch.setattr(main_mod, "initialize_schema", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_phase8_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_risk_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_decision_funnel_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_ml_feature_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_restart_state_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_evidence_partition_columns", lambda: None)
    monkeypatch.setattr(main_mod, "init_tracing", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_mod, "init_metrics", lambda: object())
    monkeypatch.setattr(main_mod, "MT5Connection", _Bridge)
    monkeypatch.setattr(main_mod, "Engine", _Engine)

    exit_code = main_mod.main(["--mode", "smoke", "--policy-mode", "legacy_micro_capital"])

    assert exit_code == 0
    assert created["use_mock"] is True
    assert created["run_mode"] == "smoke"
    assert created["run"] == ("smoke", 0)
    assert created["policy"]["MODE_ID"] == "legacy_micro_capital"


def test_main_blocks_non_srs_policy_on_live_account_even_with_approval(monkeypatch) -> None:
    risk_events = []

    class _Creds:
        login = 123
        password = "x"
        server = "real"

    class _Bridge:
        def __init__(self, login, password, server):
            self.login = login
            self.password = password
            self.server = server

        def connect(self) -> bool:
            return True

        def is_demo_account(self) -> bool:
            return False

        def shutdown(self) -> None:
            return None

    monkeypatch.delenv("USE_MT5_MOCK", raising=False)
    monkeypatch.setenv("FX_ALLOW_NON_SRS_POLICY", "1")
    monkeypatch.setattr(main_mod, "load_runtime_env", lambda: None)
    monkeypatch.setattr(main_mod, "load_mt5_credentials_from_env", lambda: _Creds)
    monkeypatch.setattr(main_mod, "initialize_schema", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_phase8_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_risk_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_decision_funnel_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_ml_feature_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_restart_state_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_evidence_partition_columns", lambda: None)
    monkeypatch.setattr(main_mod, "init_tracing", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_mod, "init_metrics", lambda: object())
    monkeypatch.setattr(main_mod, "MT5Connection", _Bridge)
    monkeypatch.setattr(main_mod, "insert_risk_event", lambda *args: risk_events.append(args))
    monkeypatch.setattr(main_mod, "Engine", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Engine should not start")))

    exit_code = main_mod.main(["--mode", "smoke", "--policy-mode", "legacy_micro_capital"])

    assert exit_code == 4
    assert risk_events[-1][0] == "NON_SRS_POLICY_APPROVAL"
    assert risk_events[-1][1] == "BLOCK"
    assert "require use_mt5_mock=1 or a verified demo account" in risk_events[-1][2].lower()


def test_main_disables_demo_only_experiment_on_non_demo_account(monkeypatch) -> None:
    created: dict[str, object] = {}

    class _Creds:
        login = 123
        password = "x"
        server = "real"

    class _Bridge:
        def __init__(self, login, password, server):
            self.login = login
            self.password = password
            self.server = server

        def connect(self) -> bool:
            return True

        def is_demo_account(self) -> bool:
            return False

        def shutdown(self) -> None:
            return None

    class _Engine:
        def __init__(self, bridge, tracer, metrics, *, use_mock, run_mode, policy):
            created["policy"] = policy

        def run(self, mode: str, iterations: int = 0) -> None:
            created["run"] = (mode, iterations)

    monkeypatch.delenv("USE_MT5_MOCK", raising=False)
    monkeypatch.setenv("FX_EXPERIMENT_AUDUSD_PULLBACK_RELAX", "1")
    monkeypatch.setattr(main_mod, "load_runtime_env", lambda: None)
    monkeypatch.setattr(main_mod, "initialize_schema", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_phase8_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_risk_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_decision_funnel_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_ml_feature_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_restart_state_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_evidence_partition_columns", lambda: None)
    monkeypatch.setattr(main_mod, "load_mt5_credentials_from_env", lambda: _Creds())
    monkeypatch.setattr(main_mod, "init_tracing", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_mod, "init_metrics", lambda: object())
    monkeypatch.setattr(main_mod, "MT5Connection", _Bridge)
    monkeypatch.setattr(main_mod, "Engine", _Engine)

    exit_code = main_mod.main(["--mode", "demo", "--policy-mode", "core_srs"])

    assert exit_code == 0
    assert created["run"] == ("demo", 0)
    assert created["policy"]["EXPERIMENTS"]["AUDUSD_PULLBACK_RELAX"]["enabled"] is False
    assert "EXPERIMENT_TAG" not in created["policy"]


def test_main_keeps_demo_only_experiment_on_demo_account(monkeypatch) -> None:
    created: dict[str, object] = {}

    class _Creds:
        login = 123
        password = "x"
        server = "demo"

    class _Bridge:
        def __init__(self, login, password, server):
            self.login = login
            self.password = password
            self.server = server

        def connect(self) -> bool:
            return True

        def is_demo_account(self) -> bool:
            return True

        def shutdown(self) -> None:
            return None

    class _Engine:
        def __init__(self, bridge, tracer, metrics, *, use_mock, run_mode, policy):
            created["policy"] = policy

        def run(self, mode: str, iterations: int = 0) -> None:
            created["run"] = (mode, iterations)

    monkeypatch.delenv("USE_MT5_MOCK", raising=False)
    monkeypatch.setenv("FX_EXPERIMENT_AUDUSD_PULLBACK_RELAX", "1")
    monkeypatch.setattr(main_mod, "load_runtime_env", lambda: None)
    monkeypatch.setattr(main_mod, "initialize_schema", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_phase8_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_risk_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_decision_funnel_events", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_ml_feature_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_restart_state_columns", lambda: None)
    monkeypatch.setattr(main_mod, "migrate_add_evidence_partition_columns", lambda: None)
    monkeypatch.setattr(main_mod, "load_mt5_credentials_from_env", lambda: _Creds())
    monkeypatch.setattr(main_mod, "init_tracing", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(main_mod, "init_metrics", lambda: object())
    monkeypatch.setattr(main_mod, "MT5Connection", _Bridge)
    monkeypatch.setattr(main_mod, "Engine", _Engine)

    exit_code = main_mod.main(["--mode", "demo", "--policy-mode", "core_srs"])

    assert exit_code == 0
    assert created["run"] == ("demo", 0)
    assert created["policy"]["EXPERIMENTS"]["AUDUSD_PULLBACK_RELAX"]["enabled"] is True
    assert created["policy"]["EXPERIMENT_TAG"] == "audusd_pullback_relax"
