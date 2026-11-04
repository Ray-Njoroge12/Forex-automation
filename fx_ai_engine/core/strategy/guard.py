"""SignalGuard: checks every signal a strategy returns, live and in replay alike.

``registry.build_strategy`` always wraps the strategy in ``GuardedStrategy``.
The guard owns the SRS minimum R:R (SRS 5.3) for every strategy, because no
downstream component checks it. A failed rule turns the decision into a
TECHNICAL reject; an exception (in the strategy or in the guard) either
propagates (``on_error="raise"``: replay and tests) or becomes a fail-closed
``STRATEGY_ERROR`` decision (``on_error="reject"``: live).
"""
from __future__ import annotations

import logging
import math
from dataclasses import replace
from typing import Any

from core.strategy.base import (
    STRATEGY_ERROR, Strategy, StrategyDecision, regime_rejected, utc_now_iso,
)
from core.types import RegimeOutput, TechnicalSignal

logger = logging.getLogger("fx_ai_engine.strategy")

SIGNAL_INVALID = "STRATEGY_SIGNAL_INVALID"
ORDER_TYPE_UNSUPPORTED = "STRATEGY_ORDER_TYPE_UNSUPPORTED"
TP_MODES = frozenset({"FIXED", "TRAIL", "HYBRID"})
RR_ROUNDING_TOLERANCE_PIPS = 0.02
ON_ERROR_MODES = ("raise", "reject")
ERROR_DETAIL_MAX = 200


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def check_signal(signal: Any, *, symbol: str, min_rr: float) -> tuple[str, str] | None:
    """Return ``(reason_code, details)`` for the first rule the signal breaks, else ``None``."""
    def fail(field: str, value: Any, rule: str, reason: str = SIGNAL_INVALID) -> tuple[str, str]:
        return reason, f"field={field} value={value} rule={rule}"

    if not isinstance(signal, TechnicalSignal):
        return fail("signal", type(signal).__name__, "type=TechnicalSignal")
    if signal.symbol != symbol:
        return fail("symbol", signal.symbol, f"equals={symbol}")
    if signal.direction not in {"BUY", "SELL"}:
        return fail("direction", signal.direction, "in=BUY,SELL")
    for name in ("stop_pips", "take_profit_pips"):
        value = getattr(signal, name)
        if not _finite(value) or float(value) <= 0:
            return fail(name, value, "finite>0")
    if not _finite(signal.risk_reward) or float(signal.risk_reward) < min_rr:
        return fail("risk_reward", signal.risk_reward, f"finite>={min_rr:g}")
    floor = min_rr * float(signal.stop_pips) - RR_ROUNDING_TOLERANCE_PIPS
    if float(signal.take_profit_pips) < floor:
        return fail("take_profit_pips", signal.take_profit_pips, f">=min_rr*stop_pips-{RR_ROUNDING_TOLERANCE_PIPS:g}")
    if signal.order_type != "MARKET":
        return fail("order_type", signal.order_type, "equals=MARKET", ORDER_TYPE_UNSUPPORTED)
    if signal.tp_mode not in TP_MODES:
        return fail("tp_mode", signal.tp_mode, "in=FIXED,TRAIL,HYBRID")
    for name in ("be_trigger_r", "partial_close_r", "trailing_atr_mult"):
        value = getattr(signal, name)
        if not _finite(value) or float(value) < 0:
            return fail(name, value, "finite>=0")
    return None


def error_decision(strategy_id: str, exc: BaseException) -> StrategyDecision:
    """Fail-closed decision for an exception: REGIME reject ``STRATEGY_ERROR``, no signal."""
    details = f"{type(exc).__name__}: {exc}"[:ERROR_DETAIL_MAX]
    regime = RegimeOutput(regime="NO_TRADE", trend_state="UNKNOWN", volatility_state="UNKNOWN", confidence=0.0,
                          reason_code=STRATEGY_ERROR, timestamp_utc=utc_now_iso())
    return regime_rejected(strategy_id, regime, STRATEGY_ERROR, details)


class GuardedStrategy:
    """Wraps a strategy; the only object ``build_strategy`` hands to callers."""

    def __init__(self, inner: Strategy, *, min_rr: float, on_error: str = "raise"):
        if on_error not in ON_ERROR_MODES:
            raise ValueError(f"on_error must be one of {ON_ERROR_MODES}, got {on_error!r}")
        self.inner = inner
        self.strategy_id = inner.strategy_id
        self.symbol = inner.symbol
        self.min_rr = float(min_rr)
        self.on_error = on_error

    def evaluate(self) -> StrategyDecision:
        try:
            decision = self.inner.evaluate()
            if not isinstance(decision, StrategyDecision):
                raise TypeError(f"strategy returned {type(decision).__name__}, expected StrategyDecision")
            if decision.signal is None:
                return decision
            failure = check_signal(decision.signal, symbol=self.symbol, min_rr=self.min_rr)
            if failure is None:
                return decision
            reason, details = failure
            if self.on_error == "reject":
                logger.warning("Strategy %s %s signal rejected by guard: %s %s",
                               self.strategy_id, self.symbol, reason, details)
            return replace(decision, signal=None, reason_code=reason, details=details)
        except Exception as exc:
            if self.on_error == "raise":
                raise
            logger.error("Strategy %s %s raised; no signal this bar (fail closed)",
                         self.strategy_id, self.symbol, exc_info=True)
            return error_decision(self.strategy_id, exc)
