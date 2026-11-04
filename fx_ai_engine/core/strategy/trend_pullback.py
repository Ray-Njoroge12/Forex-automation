"""Default strategy ``trend_pullback_v1``: the RegimeAgent + TechnicalAgent rules traded since v1.

Built and called exactly as ``main.Engine`` and ``generate_candidates`` did before
stage D: same constructors, same timeframe codes, same order. The only live
strategy (``main.LIVE_STRATEGY_ID``). ``core/agents/**`` is wrapped, not edited.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.strategy.base import (
    TIMEFRAME_H1, TIMEFRAME_M15, TRENDING_REGIMES, FetchOHLC, FetchSpread, StrategyDecision, regime_rejected,
)
from core.strategy.registry import DEFAULT_STRATEGY_ID, register_strategy
from core.types import RegimeOutput


class TrendPullbackStrategy:
    strategy_id = DEFAULT_STRATEGY_ID

    def __init__(self, symbol: str, fetch_ohlc: FetchOHLC, fetch_spread: FetchSpread | None,
                 policy: Mapping[str, object], *, regime_cache_key: Callable[[], Any] | None = None):
        self.symbol = symbol
        self.regime_agent = RegimeAgent(symbol, fetch_ohlc, policy=policy)
        self.technical_agent = TechnicalAgent(symbol, fetch_ohlc, fetch_spread, policy=policy)
        self.regime_cache_key = regime_cache_key
        self._cache_key: Any = None
        self._cached: tuple[RegimeOutput, str] | None = None

    def _regime(self) -> tuple[RegimeOutput, str]:
        if self.regime_cache_key is None:
            regime = self.regime_agent.evaluate(TIMEFRAME_H1)
            return regime, self.regime_agent.last_details
        key = self.regime_cache_key()
        if self._cached is None or key != self._cache_key:
            regime = self.regime_agent.evaluate(TIMEFRAME_H1)
            self._cached = (regime, self.regime_agent.last_details)
            self._cache_key = key
        return self._cached

    def evaluate(self) -> StrategyDecision:
        regime, regime_details = self._regime()
        if regime.regime not in TRENDING_REGIMES:
            return regime_rejected(self.strategy_id, regime, regime.reason_code, regime_details)
        signal = self.technical_agent.evaluate(regime, TIMEFRAME_M15, TIMEFRAME_H1)
        reason = signal.reason_code if signal is not None else self.technical_agent.last_reason_code
        return StrategyDecision(
            strategy_id=self.strategy_id,
            regime=regime,
            regime_passed=True,
            regime_reason_code=regime.reason_code,
            regime_details=regime_details,
            signal=signal,
            reason_code=reason,
            details=self.technical_agent.last_details,
        )


def _factory(*, symbol, fetch_ohlc, fetch_spread, policy, params, regime_cache_key):
    return TrendPullbackStrategy(symbol, fetch_ohlc, fetch_spread, policy, regime_cache_key=regime_cache_key)


register_strategy(DEFAULT_STRATEGY_ID, _factory, None, live_eligible=True)
