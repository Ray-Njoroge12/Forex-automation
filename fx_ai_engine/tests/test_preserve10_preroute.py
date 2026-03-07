from __future__ import annotations

from contextlib import nullcontext

import pandas as pd

import main as main_mod
from bridge.signal_router import RouterCleanupResult, SignalRouteError
from core.mt5_bridge import TradeFeasibilityDecision
from core.types import AdversarialDecision, RegimeOutput, TechnicalSignal


class _FakeTracer:
    def start_as_current_span(self, _name: str):
        return nullcontext()


class _FakeMetrics:
    def inc(self, _name: str) -> None:
        return None

    def set_gauge(self, _name: str, _value: float) -> None:
        return None


class _StaticEvaluator:
    def __init__(self, output):
        self.output = output

    def evaluate(self, *args, **kwargs):
        return self.output


class _FakeBridge:
    def __init__(self, feasibility: TradeFeasibilityDecision):
        self.feasibility = feasibility
        self.calls: list[tuple[str, float, float, float | None]] = []

    def fetch_ohlc_data(self, *_args, **_kwargs):
        return pd.DataFrame()

    def get_live_spread(self, _symbol: str) -> float:
        return 0.0001

    def get_account_snapshot(self):
        return None

    def evaluate_trade_feasibility(self, symbol: str, risk_percent: float, stop_pips: float, *, account_balance=None):
        self.calls.append((symbol, risk_percent, stop_pips, account_balance))
        return self.feasibility


class _FakeRanker:
    def load(self) -> bool:
        return False

    def predict_proba(self, _features: dict[str, float]) -> float:
        return 0.5


def _build_engine(monkeypatch, tmp_path, *, mode: str | None, feasibility: TradeFeasibilityDecision):
    for name in ("FX_POLICY_MODE", "MICRO_CAPITAL_MODE", "FIXED_RISK_USD"):
        monkeypatch.delenv(name, raising=False)
    if mode is not None:
        monkeypatch.setenv("FX_POLICY_MODE", mode)

    for folder in ("pending_signals", "feedback", "exits", "active_locks"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(main_mod, "get_mt5_bridge_path", lambda: tmp_path)
    monkeypatch.setattr(main_mod, "load_calendar", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main_mod, "load_rate_differentials", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main_mod, "SentimentAgent", lambda: object())
    monkeypatch.setattr(main_mod, "SignalRanker", _FakeRanker)

    bridge = _FakeBridge(feasibility)
    engine = main_mod.Engine(bridge=bridge, tracer=_FakeTracer(), metrics=_FakeMetrics(), use_mock=True)
    engine.account_status.balance = 10.0

    regime = RegimeOutput(
        regime="TRENDING_BULL",
        trend_state="BULLISH",
        volatility_state="NORMAL",
        confidence=0.8,
        reason_code="REGIME_TRENDING_BULL",
        timestamp_utc="2026-03-06T12:00:00+00:00",
        atr_ratio=1.0,
    )
    technical = TechnicalSignal(
        trade_id="AI_preserve10_001",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=20.0,
        take_profit_pips=44.0,
        risk_reward=2.2,
        confidence=0.72,
        reason_code="TECH_PULLBACK_BUY",
        timestamp_utc="2026-03-06T12:00:00+00:00",
        spread_entry=1.0,
    )
    adversarial = AdversarialDecision(
        approved=True,
        risk_modifier=1.0,
        reason_code="ADV_APPROVED",
        details="ok",
        timestamp_utc="2026-03-06T12:00:00+00:00",
    )
    engine.agents["EURUSD"] = {
        "regime": _StaticEvaluator(regime),
        "technical": _StaticEvaluator(technical),
        "adversarial": _StaticEvaluator(adversarial),
    }
    return engine, bridge


def test_preserve_10_rejects_infeasible_trade_before_router(monkeypatch, tmp_path) -> None:
    proposals = []
    risk_events = []
    sent_payloads = []
    monkeypatch.setattr(main_mod, "insert_trade_proposal", lambda *args, **kwargs: proposals.append(kwargs))
    monkeypatch.setattr(main_mod, "insert_risk_event", lambda *args: risk_events.append(args))

    engine, bridge = _build_engine(
        monkeypatch,
        tmp_path,
        mode="preserve_10",
        feasibility=TradeFeasibilityDecision(
            can_assess=True,
            approved=False,
            reason_code="REJECTED_LOT_PREROUTE",
            details="raw_limit=0.002500 min_lot=0.0100",
        ),
    )
    engine.router.send = lambda payload: sent_payloads.append(payload)

    engine._evaluate_symbol("EURUSD")

    assert sent_payloads == []
    assert proposals[-1]["status"] == "REJECTED"
    assert proposals[-1]["reason_code"] == "REJECTED_LOT_PREROUTE"
    assert risk_events[-1][0] == "PRE_ROUTE_FEASIBILITY"
    assert "refused before MT5 routing" in risk_events[-1][2]
    assert "trade blocked before MT5 routing" in risk_events[-1][2]
    assert "evidence=Preserve-$10 doctrine" in risk_events[-1][2]
    assert bridge.calls and bridge.calls[-1][3] == 10.0


def test_preserve_10_rejects_unassessable_trade_before_router(monkeypatch, tmp_path) -> None:
    proposals = []
    risk_events = []
    sent_payloads = []
    monkeypatch.setattr(main_mod, "insert_trade_proposal", lambda *args, **kwargs: proposals.append(kwargs))
    monkeypatch.setattr(main_mod, "insert_risk_event", lambda *args: risk_events.append(args))

    engine, bridge = _build_engine(
        monkeypatch,
        tmp_path,
        mode="preserve_10",
        feasibility=TradeFeasibilityDecision(
            can_assess=False,
            approved=True,
            reason_code="BROKER_CONTRACT_UNAVAILABLE",
            details="symbol execution contract unavailable for symbol=EURUSD",
        ),
    )
    engine.router.send = lambda payload: sent_payloads.append(payload)

    engine._evaluate_symbol("EURUSD")

    assert sent_payloads == []
    assert proposals[-1]["status"] == "REJECTED"
    assert proposals[-1]["reason_code"] == "BROKER_CONTRACT_UNAVAILABLE"
    assert risk_events[-1][0] == "PRE_ROUTE_FEASIBILITY"
    assert "blocked before MT5 routing" in risk_events[-1][2]
    assert "contract data is unavailable" in risk_events[-1][2]
    assert "evidence=Preserve-$10 doctrine" in risk_events[-1][2]
    assert bridge.calls and bridge.calls[-1][3] == 10.0


def test_preserve_10_routes_feasible_trade_without_preroute_warning(monkeypatch, tmp_path) -> None:
    proposals = []
    risk_events = []
    sent_payloads = []
    monkeypatch.setattr(main_mod, "insert_trade_proposal", lambda *args, **kwargs: proposals.append(kwargs))
    monkeypatch.setattr(main_mod, "insert_risk_event", lambda *args: risk_events.append(args))

    engine, bridge = _build_engine(
        monkeypatch,
        tmp_path,
        mode="preserve_10",
        feasibility=TradeFeasibilityDecision(
            can_assess=True,
            approved=True,
            reason_code="TRADE_FEASIBLE",
            details="quantized_lot=0.010000",
        ),
    )
    engine.router.send = lambda payload: sent_payloads.append(payload)

    engine._evaluate_symbol("EURUSD")

    assert len(sent_payloads) == 1
    assert proposals[-1]["status"] == "PENDING"
    assert proposals[-1]["reason_code"] == "ROUTED_TO_MT5"
    assert all(event[0] != "PRE_ROUTE_FEASIBILITY" for event in risk_events)
    assert bridge.calls and bridge.calls[-1][3] == 10.0


def test_core_srs_bypasses_preserve_10_preroute_gate(monkeypatch, tmp_path) -> None:
    proposals = []
    risk_events = []
    sent_payloads = []
    monkeypatch.setattr(main_mod, "insert_trade_proposal", lambda *args, **kwargs: proposals.append(kwargs))
    monkeypatch.setattr(main_mod, "insert_risk_event", lambda *args: risk_events.append(args))

    engine, bridge = _build_engine(
        monkeypatch,
        tmp_path,
        mode=None,
        feasibility=TradeFeasibilityDecision(
            can_assess=True,
            approved=False,
            reason_code="REJECTED_LOT_PREROUTE",
            details="raw_limit=0.002500 min_lot=0.0100",
        ),
    )
    engine.router.send = lambda payload: sent_payloads.append(payload)

    engine._evaluate_symbol("EURUSD")

    assert len(sent_payloads) == 1
    assert proposals[-1]["status"] == "PENDING"
    assert proposals[-1]["reason_code"] == "ROUTED_TO_MT5"
    assert bridge.calls == []
    assert all(event[0] != "PRE_ROUTE_FEASIBILITY" for event in risk_events)


def test_preserve_10_halts_on_router_publish_uncertainty(monkeypatch, tmp_path) -> None:
    proposals = []
    risk_events = []
    monkeypatch.setattr(main_mod, "insert_trade_proposal", lambda *args, **kwargs: proposals.append(kwargs))
    monkeypatch.setattr(main_mod, "insert_risk_event", lambda *args: risk_events.append(args))

    engine, _bridge = _build_engine(
        monkeypatch,
        tmp_path,
        mode="preserve_10",
        feasibility=TradeFeasibilityDecision(
            can_assess=True,
            approved=True,
            reason_code="TRADE_FEASIBLE",
            details="quantized_lot=0.010000",
        ),
    )
    engine.router.send = lambda _payload: (_ for _ in ()).throw(
        SignalRouteError(
            trade_id="AI_preserve10_001",
            pending_written=True,
            detail="router failed after publish; preserve-first reconciliation required",
        )
    )

    engine._evaluate_symbol("EURUSD")

    assert proposals[-1]["status"] == "EXECUTION_UNCERTAIN"
    assert proposals[-1]["reason_code"] == "ROUTER_SEND_UNCERTAIN"
    assert risk_events[-1][0] == "PRESERVE_10_BRIDGE_UNCERTAIN"
    assert risk_events[-1][1] == "BLOCK"
    assert engine.account_status.is_trading_halted is True
    assert engine.account_status.state_reconciled is False


def test_preserve_10_halts_when_router_housekeeping_finds_uncertain_artifacts(monkeypatch, tmp_path) -> None:
    risk_events = []
    uncertain_marks = []
    evaluated = []
    monkeypatch.setattr(main_mod, "insert_risk_event", lambda *args: risk_events.append(args))
    monkeypatch.setattr(main_mod, "mark_trade_execution_uncertain", lambda *args: uncertain_marks.append(args))
    monkeypatch.setattr(main_mod, "is_tradeable_session", lambda _now: True)

    engine, _bridge = _build_engine(
        monkeypatch,
        tmp_path,
        mode="preserve_10",
        feasibility=TradeFeasibilityDecision(
            can_assess=True,
            approved=True,
            reason_code="TRADE_FEASIBLE",
            details="quantized_lot=0.010000",
        ),
    )
    engine.router.cleanup_stale = lambda max_age_seconds=600: RouterCleanupResult(
        stale_pending_trade_ids=("AI_stale_001",),
        orphan_lock_trade_ids=("AI_orphan_001",),
    )
    engine._evaluate_symbol = lambda sym: evaluated.append(sym)

    engine._decision_cycle()

    assert uncertain_marks == [
        ("AI_stale_001", "ROUTER_PENDING_UNCERTAIN"),
        ("AI_orphan_001", "ROUTER_LOCK_UNCERTAIN"),
    ]
    assert evaluated == []
    assert risk_events[0][0] == "PRESERVE_10_BRIDGE_UNCERTAIN"
    assert risk_events[1][0] == "PRESERVE_10_BRIDGE_UNCERTAIN"
    assert engine.account_status.is_trading_halted is True
    assert engine.account_status.state_reconciled is False