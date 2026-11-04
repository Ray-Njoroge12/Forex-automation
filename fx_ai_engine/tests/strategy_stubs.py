"""Stub strategies and a registry fixture shared by the strategy interface and wiring tests."""
from __future__ import annotations

import pytest

from core.strategy import StrategyDecision, registry
from core.strategy.base import TIMEFRAME_M15, regime_rejected
from core.types import RegimeOutput, TechnicalSignal

REGIME_BULL = RegimeOutput("TRENDING_BULL", "BULLISH", "NORMAL", 0.8, "REGIME_TREND_BULL", "t", 1.0)


@pytest.fixture()
def isolated_registry(monkeypatch):
    """Strategies registered in a test disappear afterwards."""
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    return registry


def _signal(**overrides) -> TechnicalSignal:
    base = dict(trade_id="AI_X", symbol="GBPUSD", direction="BUY", stop_pips=25.0, take_profit_pips=55.0,
                risk_reward=2.2, confidence=0.5, reason_code="STUB_BUY", timestamp_utc="t",
                be_trigger_r=0.0, partial_close_r=0.0, trailing_atr_mult=0.0, tp_mode="FIXED")
    base.update(overrides)
    return TechnicalSignal(**base)


class StubStrategy:
    """Fetches M15 (so R1 can observe fetch lengths) and returns ``make(self)``."""

    def __init__(self, strategy_id, symbol, fetch_ohlc, make):
        self.strategy_id, self.symbol, self.fetch_ohlc, self.make = strategy_id, symbol, fetch_ohlc, make

    def evaluate(self) -> StrategyDecision:
        self.fetch_ohlc(self.symbol, TIMEFRAME_M15, 50)
        return self.make(self)


def _always_signal(s: StubStrategy) -> StrategyDecision:
    sig = _signal(symbol=s.symbol)
    return StrategyDecision(s.strategy_id, REGIME_BULL, True, "STUB_REGIME_OK", "", sig, sig.reason_code, "stub")


def _regime_reject(s: StubStrategy) -> StrategyDecision:
    regime = RegimeOutput("NO_TRADE", "FLAT", "NORMAL", 0.0, "STUB_REGIME_REJECT", "t")
    return regime_rejected(s.strategy_id, regime, "STUB_REGIME_REJECT", "stub details")


def _raise(_s: StubStrategy) -> StrategyDecision:
    raise RuntimeError("stub strategy failure " + "x" * 300)


def _register_stub(reg, strategy_id: str, make, *, live_eligible: bool = True) -> None:
    def factory(*, symbol, fetch_ohlc, fetch_spread, policy, params, regime_cache_key):
        return StubStrategy(strategy_id, symbol, fetch_ohlc, make)

    reg.register_strategy(strategy_id, factory, None, live_eligible=live_eligible,
                          live_block_reason="" if live_eligible else "test stub")
