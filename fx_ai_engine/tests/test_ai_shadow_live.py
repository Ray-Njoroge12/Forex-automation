"""Live shadow logging of the typed AI verdict and engine-level invariant 3 (stage E2).

The engine runs on a temporary file bridge (never the live terminal folder).
Property: for 200 seeded cases of random rule outcomes, a random shadow model
and a random SignalRanker, routing and trade-proposal statuses are identical
with the shadow runtime enabled and disabled, and no case routes a signal any
rule rejected. The strict canary is left out on purpose: it can block a route
when enabled (docs/STATUS.md open item, owner decision).
"""
from __future__ import annotations

import json
import math
import random
from types import SimpleNamespace

import pytest

from application.routing_service import ReservationRejected
from core.policy.ai_verdict import apply_ai_verdict, verdict_from_shadow
from core.strategy import StrategyDecision
from core.types import RegimeOutput
from database.repositories.memory import InMemoryEngineRepository
from ml.meta_labeler.shadow_runtime import (
    CanaryRuntimeConfig,
    MetaLabelerShadowRuntime,
    ShadowRuntimeConfig,
)
from tests.engine_harness import make_file_bridge_engine
from tests.strategy_stubs import REGIME_BULL, _signal
from tests.test_contract_engine import (
    GOLDEN,
    NOW_FIXED,
    _clock_modules,
    _decision_record,
    _freeze_engine_clock,
    _golden_bridge,
    _synthetic_calendar,
)
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)

CASES = 200
SEED = 20261104
SYMBOLS = ("GBPUSD", "AUDUSD")  # the golden bridge has full bar history for these two
REGIME_FLAT = RegimeOutput("NO_TRADE", "FLAT", "NORMAL", 0.0, "STUB_REGIME_REJECT", "t")


class _ProbabilityModel:
    def __init__(self, probability):
        self.probability = probability

    def predict_proba(self, rows):
        if self.probability == "raise":
            raise RuntimeError("stub model failure")
        return [[1.0 - self.probability, self.probability] for _ in rows]


def _quiet_alerts(monkeypatch) -> None:
    for module in _clock_modules():
        for name in ("alert_risk_halt", "alert_trade_execution", "alert_trade_exit"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, lambda *a, **k: None)


def _engine(tmp_path, monkeypatch, repository=None):
    bridge_dir = _golden_bridge(tmp_path / "bridge")
    _freeze_engine_clock(monkeypatch)
    _quiet_alerts(monkeypatch)
    engine = make_file_bridge_engine(bridge_dir, monkeypatch, clock=lambda: NOW_FIXED, repository=repository)
    engine._calendar_events = _synthetic_calendar()
    engine.ranker_loaded = False
    engine._update_account_state()
    return engine


def _random_case(rng: random.Random, i: int) -> dict:
    def gate(p_pass: float) -> bool:
        return rng.random() < p_pass

    return {
        "symbol": rng.choice(SYMBOLS),
        "trade_id": f"AI_PROP_{i:04d}",
        "direction": rng.choice(["BUY", "SELL"]),
        "regime_passed": gate(0.85),
        "has_signal": gate(0.85),
        "adversarial": gate(0.8),
        "portfolio": gate(0.8),
        "hard_risk": gate(0.8),
        "final_risk": rng.choice([0.032, rng.uniform(0.001, 0.05)]),
        "throttle": rng.choice([1.0, 1.0, 0.5, 0.25]),
        "ranker_loaded": gate(0.5),
        "ranker_prob": rng.choice([float("nan"), rng.random(), rng.random(), 0.0, 1.0]),
        "shadow_prob": rng.choice([float("nan"), "raise", 1.5, -0.2, rng.random(), rng.random(), rng.random()]),
        "shadow_threshold": rng.choice([0.0, 0.55, 1.0, rng.random()]),
        "shadow_has_model": gate(0.85),
        "canary": rng.choice([CanaryRuntimeConfig(), CanaryRuntimeConfig(enabled=True, mode="soft"),
                              CanaryRuntimeConfig(enabled=True, mode="strict", kill_switch=True),
                              CanaryRuntimeConfig(enabled=False, mode="strict")]),
        "reservation_rejected": gate(0.15),
    }


def _install_case(engine, case: dict, *, shadow_enabled: bool, routes: list, proposals: list) -> None:
    sym = case["symbol"]
    signal = _signal(symbol=sym, trade_id=case["trade_id"], direction=case["direction"])
    if not case["regime_passed"]:
        decision = StrategyDecision("stub_ai", REGIME_FLAT, False, "STUB_REGIME_REJECT", "", None,
                                    "TECH_SKIPPED_REGIME_REJECTED", "")
    elif not case["has_signal"]:
        decision = StrategyDecision("stub_ai", REGIME_BULL, True, "STUB_REGIME_OK", "", None, "STUB_NO_SIGNAL", "")
    else:
        decision = StrategyDecision("stub_ai", REGIME_BULL, True, "STUB_REGIME_OK", "", signal, signal.reason_code, "")
    engine.strategies[sym] = SimpleNamespace(strategy_id="stub_ai", symbol=sym, evaluate=lambda: decision)
    engine.agents[sym]["adversarial"] = SimpleNamespace(evaluate=lambda *a, **k: SimpleNamespace(
        approved=case["adversarial"], reason_code="ADV_OK" if case["adversarial"] else "ADV_STUB_REJECT",
        details="stub"))
    engine.portfolio_manager = SimpleNamespace(evaluate=lambda *a, **k: SimpleNamespace(
        approved=case["portfolio"], reason_code="PM_APPROVED" if case["portfolio"] else "PM_STUB_REJECT",
        details="stub", final_risk_percent=case["final_risk"]))
    engine.hard_risk = SimpleNamespace(validate=lambda *a, **k: SimpleNamespace(
        approved=case["hard_risk"], reason_code="RISK_OK" if case["hard_risk"] else "RISK_STUB_BLOCK",
        details="stub", risk_throttle_multiplier=case["throttle"]))
    engine.ranker = SimpleNamespace(predict_proba=lambda _features: case["ranker_prob"])
    engine.ranker_loaded = case["ranker_loaded"]
    engine.canary_config = case["canary"]
    model = _ProbabilityModel(case["shadow_prob"]) if case["shadow_has_model"] else None
    engine.shadow_runtime = MetaLabelerShadowRuntime(
        config=ShadowRuntimeConfig(enabled=shadow_enabled, threshold=case["shadow_threshold"]),
        fetch_ohlc=engine.bridge.fetch_ohlc_data,
        model=model,
    )

    def reserve_and_route(payload, *, account_status):
        if case["reservation_rejected"]:
            raise ReservationRejected("RESERVATION_CAPACITY_UNAVAILABLE")
        routes.append((payload["trade_id"], payload["symbol"], payload["risk_percent"]))

    engine.routing_service = SimpleNamespace(reserve_and_route=reserve_and_route)
    engine._insert_trade_proposal = lambda technical, **kw: proposals.append(
        (technical.trade_id, kw["status"], kw["reason_code"], kw["risk_percent"]))


def _run(engine, case: dict, *, shadow_enabled: bool) -> tuple[list, list]:
    routes: list = []
    proposals: list = []
    _install_case(engine, case, shadow_enabled=shadow_enabled, routes=routes, proposals=proposals)
    engine._evaluate_symbol(case["symbol"], NOW_FIXED, "london")
    return routes, proposals


def _rules_approved(case: dict) -> bool:
    return all(case[k] for k in ("regime_passed", "has_signal", "adversarial", "portfolio", "hard_risk"))


def test_shadow_never_changes_routing_and_never_routes_a_rule_rejected_signal(tmp_path, monkeypatch, isolated_db):
    repository = InMemoryEngineRepository()
    engine = _engine(tmp_path, monkeypatch, repository=repository)
    rng = random.Random(SEED)
    cases = [_random_case(rng, i) for i in range(CASES)]
    routed_cases = shadow_rows_expected = 0
    for case in cases:
        before = len(repository.ml_shadow_events)
        off = _run(engine, case, shadow_enabled=False)
        assert len(repository.ml_shadow_events) == before
        on = _run(engine, case, shadow_enabled=True)
        assert on == off, case
        routes, proposals = on
        if routes:
            routed_cases += 1
            assert _rules_approved(case), case
            ((trade_id, _, risk_percent),) = routes
            assert trade_id == case["trade_id"]
            assert risk_percent == round(case["final_risk"] * case["throttle"], 8)
            assert [p[1] for p in proposals] == ["PENDING"]
        else:
            assert all(p[1] == "REJECTED" and p[3] == 0.0 for p in proposals), proposals
        if _rules_approved(case):
            shadow_rows_expected += 1
            row = repository.ml_shadow_events[-1]
            assert len(repository.ml_shadow_events) == before + 1
            assert row["trade_id"] == case["trade_id"] and row["strategy_id"] == "stub_ai"
            rule_risk = round(case["final_risk"] * case["throttle"], 8)
            assert row["veto"] == int(row["shadow_outcome"] == "REJECT")
            assert 0.0 <= row["would_be_risk_percent"] <= rule_risk
            assert row["latency_ms"] >= 0.0 and math.isfinite(row["latency_ms"])
        else:
            assert len(repository.ml_shadow_events) == before
    # The seeded cases exercise both routed and rejected paths and every shadow outcome.
    assert 0 < routed_cases < CASES and shadow_rows_expected > 0
    outcomes = {row["shadow_outcome"] for row in repository.ml_shadow_events}
    assert {"PASS", "REJECT", "BYPASS"} <= outcomes


def test_shadow_enabled_golden_cycle_is_unchanged_and_logs_the_typed_verdict(tmp_path, monkeypatch, isolated_db):
    engine = _engine(tmp_path, monkeypatch)
    # A model that rejects everything: in shadow mode the route must still happen.
    engine.shadow_runtime = MetaLabelerShadowRuntime(
        config=ShadowRuntimeConfig(enabled=True, threshold=0.55),
        fetch_ohlc=engine.bridge.fetch_ohlc_data,
        model=_ProbabilityModel(0.05),
    )
    engine._decision_cycle()
    assert _decision_record(isolated_db) == json.loads(GOLDEN.read_text(encoding="utf-8"))
    with isolated_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM ml_shadow_events ORDER BY id")]
        routed = [dict(r) for r in conn.execute("SELECT trade_id, risk_percent FROM trades WHERE status = 'PENDING'")]
    assert rows and routed
    by_trade = {row["trade_id"]: row for row in rows}
    for trade in routed:
        row = by_trade[trade["trade_id"]]
        assert (row["shadow_outcome"], row["veto"], row["risk_multiplier"]) == ("REJECT", 1, 0.0)
        assert row["would_be_risk_percent"] == 0.0 and trade["risk_percent"] > 0.0
        assert row["primary_gate_outcome"] == "ROUTE"
        assert row["strategy_id"] == engine.strategy_id
        assert row["latency_ms"] is not None and row["latency_ms"] >= 0.0


@pytest.mark.parametrize("outcome,expected", [("PASS", (0, 1.0, 0.02)), ("BYPASS", (0, 1.0, 0.02)),
                                              ("REJECT", (1, 0.0, 0.0))])
def test_logged_would_be_risk_matches_the_combinator(outcome, expected):
    verdict = verdict_from_shadow(SimpleNamespace(outcome=outcome, reason_code="R"))
    _, would_be, _ = apply_ai_verdict(True, 0.02, verdict)
    assert (int(verdict.veto), verdict.risk_multiplier, would_be) == expected
