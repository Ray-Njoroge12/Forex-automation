from __future__ import annotations

from datetime import datetime, timezone

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

    exit_code = main_mod.main()

    assert exit_code == 3
    assert created["bridge"].events == ["connect", "approval_facts", "shutdown"]
    assert risk_events[-1][0] == "PRESERVE_10_STARTUP_APPROVAL"
    assert risk_events[-1][1] == "BLOCK"
    assert "blocked before engine start" in risk_events[-1][2].lower()
    assert "evidence=Preserve-$10 doctrine" in risk_events[-1][2]