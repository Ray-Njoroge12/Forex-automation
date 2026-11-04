"""Strategy registry: the one place strategies are declared and built (ADR 0004).

Live and replay both call ``build_strategy``, which always returns a
``GuardedStrategy``. Strategy id and parameters are arguments, never policy
keys, so the ``core_srs`` policy hash (pinned by the EA) does not change.

Live eligibility (owner decision 2026-11-04): every strategy declares
``live_eligible``; ``build_strategy(run_context="live")`` refuses an ineligible
or unknown id before its factory runs, and ``main.Engine`` checks
``require_live_eligible`` before it touches the bridge or the database.
"""
from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from core.strategy.base import FetchOHLC, FetchSpread, Strategy
from core.strategy.guard import ON_ERROR_MODES, GuardedStrategy

DEFAULT_STRATEGY_ID = "trend_pullback_v1"
RUN_CONTEXTS = ("live", "research")
STRATEGY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

Factory = Callable[..., Strategy]


class StrategyNotLiveEligibleError(ValueError):
    """The strategy id is unknown or registered ``live_eligible=False``."""

    def __init__(self, strategy_id: str, reason: str):
        super().__init__(f"strategy {strategy_id!r} is not live-eligible: {reason}")
        self.strategy_id = strategy_id
        self.reason = reason


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    factory: Factory
    params_type: type | None
    live_eligible: bool
    live_block_reason: str


_REGISTRY: dict[str, StrategySpec] = {}


def param_range(low: float, high: float) -> dict[str, Any]:
    """``dataclasses.field`` metadata declaring an inclusive range for a Params field."""
    return {"range": (low, high)}


def register_strategy(strategy_id: str, factory: Factory, params_type: type | None, *,
                      live_eligible: bool, live_block_reason: str = "") -> None:
    if not isinstance(strategy_id, str) or not STRATEGY_ID_PATTERN.match(strategy_id):
        raise ValueError(f"strategy id {strategy_id!r} does not match {STRATEGY_ID_PATTERN.pattern}")
    if strategy_id in _REGISTRY:
        raise ValueError(f"strategy {strategy_id!r} is already registered")
    if not isinstance(live_eligible, bool):
        raise ValueError(f"live_eligible for {strategy_id!r} must be a bool")
    if not live_eligible and not str(live_block_reason).strip():
        raise ValueError(f"strategy {strategy_id!r} is registered live_eligible=False without a live_block_reason")
    if params_type is not None and not (dataclasses.is_dataclass(params_type)
                                        and params_type.__dataclass_params__.frozen):
        raise ValueError(f"params_type for {strategy_id!r} must be a frozen dataclass or None")
    _REGISTRY[strategy_id] = StrategySpec(strategy_id, factory, params_type, live_eligible,
                                          "" if live_eligible else str(live_block_reason).strip())


def available_strategies() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def get_spec(strategy_id: str) -> StrategySpec:
    try:
        return _REGISTRY[strategy_id]
    except (KeyError, TypeError):
        raise ValueError(f"unknown strategy {strategy_id!r}; registered: {', '.join(available_strategies())}") from None


def is_live_eligible(strategy_id: str) -> bool:
    spec = _REGISTRY.get(strategy_id)
    return bool(spec and spec.live_eligible)


def live_block_reason(strategy_id: str) -> str:
    spec = _REGISTRY.get(strategy_id)
    if spec is None:
        return "unknown strategy id"
    return spec.live_block_reason


def require_live_eligible(strategy_id: str) -> None:
    if not is_live_eligible(strategy_id):
        raise StrategyNotLiveEligibleError(strategy_id, live_block_reason(strategy_id))


def _check_value(strategy_id: str, f: dataclasses.Field, value: Any) -> Any:
    default = f.default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"strategy {strategy_id!r} param {f.name!r} must be a number, got {value!r}")
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"strategy {strategy_id!r} param {f.name!r} must be an integer, got {value!r}")
            value = int(value)
    else:
        value = float(value)
    if not math.isfinite(float(value)):
        raise ValueError(f"strategy {strategy_id!r} param {f.name!r} must be finite, got {value!r}")
    low, high = f.metadata.get("range", (-math.inf, math.inf))
    if not low <= value <= high:
        raise ValueError(f"strategy {strategy_id!r} param {f.name!r}={value!r} outside [{low}, {high}]")
    return value


def resolve_params(strategy_id: str, params: Mapping[str, Any] | None = None) -> Any:
    """Validated Params instance (``None`` for strategies without params)."""
    spec = get_spec(strategy_id)
    params = dict(params or {})
    if spec.params_type is None:
        if params:
            raise ValueError(f"strategy {strategy_id!r} takes no params, got {sorted(params)}")
        return None
    fields = {f.name: f for f in dataclasses.fields(spec.params_type)}
    unknown = sorted(set(params) - set(fields))
    if unknown:
        raise ValueError(f"strategy {strategy_id!r} has no param(s) {unknown}; known: {sorted(fields)}")
    values = {name: _check_value(strategy_id, fields[name], value) for name, value in params.items()}
    return spec.params_type(**values)  # __post_init__ checks relations between fields


def canonical_params(strategy_id: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Params with defaults filled in, keys sorted (cache keys, reports and the registry)."""
    resolved = resolve_params(strategy_id, params)
    if resolved is None:
        return {}
    return dict(sorted(dataclasses.asdict(resolved).items()))


def build_strategy(strategy_id: str, *, symbol: str, fetch_ohlc: FetchOHLC, fetch_spread: FetchSpread | None,
                   policy: Mapping[str, object], run_context: str, params: Mapping[str, Any] | None = None,
                   regime_cache_key: Callable[[], Any] | None = None, on_error: str = "raise") -> GuardedStrategy:
    if run_context not in RUN_CONTEXTS:
        raise ValueError(f"run_context must be one of {RUN_CONTEXTS}, got {run_context!r}")
    if run_context == "live":
        require_live_eligible(strategy_id)
    spec = get_spec(strategy_id)
    if on_error not in ON_ERROR_MODES:
        raise ValueError(f"on_error must be one of {ON_ERROR_MODES}, got {on_error!r}")
    if "MIN_RISK_REWARD" not in policy:
        raise ValueError("policy has no MIN_RISK_REWARD; the strategy guard needs the SRS minimum R:R")
    min_rr = float(policy["MIN_RISK_REWARD"])
    if not math.isfinite(min_rr) or min_rr <= 0:
        raise ValueError(f"policy MIN_RISK_REWARD must be finite and > 0, got {policy['MIN_RISK_REWARD']!r}")
    resolved = resolve_params(strategy_id, params)
    inner = spec.factory(symbol=symbol, fetch_ohlc=fetch_ohlc, fetch_spread=fetch_spread, policy=policy,
                         params=resolved, regime_cache_key=regime_cache_key)
    return GuardedStrategy(inner, min_rr=min_rr, on_error=on_error)
