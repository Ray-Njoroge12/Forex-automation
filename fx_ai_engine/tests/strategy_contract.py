"""Contract every registered strategy must meet (stage D design 3.12).

``assert_strategy_contract(factory, frames, decision_times)`` evaluates the
strategy built by ``factory(fetch_ohlc, fetch_spread)`` on a point-in-time feed
at each decision time and checks:
  * typed outputs and non-empty reason codes;
  * determinism: two fresh runs give the same decision (``trade_id`` and
    ``timestamp_utc`` excluded, as in R1);
  * completed bars only: the decision is unchanged when every bar not closed by
    the decision time is removed (``research.bias_audit.truncate_frames``);
  * every fetch asks for at most ``LIVE_BAR_EXPORT_LIMIT`` bars;
  * a returned signal is a MARKET order with stop and target > 0 and
    R:R >= the policy minimum.
Returns the decisions so callers can assert on them further.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Callable, Mapping, Sequence

import pandas as pd

from backtesting.unified_replay import Frames, PointInTimeFeed, pip_size
from core.strategy import LIVE_BAR_EXPORT_LIMIT, StrategyDecision, TECH_SKIPPED_REGIME_REJECTED
from core.types import RegimeOutput, TechnicalSignal
from research.bias_audit import truncate_frames
from research.common import VOLATILE_FIELDS

StrategyFactory = Callable[..., object]
DEFAULT_SPREAD_PIPS = 0.8


def fingerprint(decision: StrategyDecision) -> dict:
    out = asdict(decision)
    out["regime"] = {k: v for k, v in out["regime"].items() if k not in VOLATILE_FIELDS}
    if out["signal"] is not None:
        out["signal"] = {k: v for k, v in out["signal"].items() if k not in VOLATILE_FIELDS}
    return out


def run_at(factory: StrategyFactory, frames: Frames, t: pd.Timestamp, *,
           spread_pips: float = DEFAULT_SPREAD_PIPS) -> tuple[StrategyDecision, list[int], object]:
    """One fresh strategy evaluated at ``t``; returns (decision, requested bar counts, strategy)."""
    feed = PointInTimeFeed(frames)
    feed.set_time(t)
    requested: list[int] = []

    def fetch(symbol: str, timeframe: int, bars: int) -> pd.DataFrame:
        requested.append(int(bars))
        return feed.fetch_ohlc(symbol, timeframe, bars)

    strategy = factory(fetch, lambda symbol: spread_pips * pip_size(symbol))
    return strategy.evaluate(), requested, strategy


def assert_strategy_contract(factory: StrategyFactory, frames: Frames, decision_times: Sequence[pd.Timestamp], *,
                             policy: Mapping[str, object] | None = None,
                             spread_pips: float = DEFAULT_SPREAD_PIPS) -> list[StrategyDecision]:
    min_rr = float((policy or {}).get("MIN_RISK_REWARD", 2.2))
    decisions = []
    assert decision_times, "contract needs at least one decision time"
    for t in decision_times:
        t = pd.Timestamp(t)
        decision, requested, strategy = run_at(factory, frames, t, spread_pips=spread_pips)
        again, _, _ = run_at(factory, frames, t, spread_pips=spread_pips)
        truncated, _, _ = run_at(factory, truncate_frames(frames, t), t, spread_pips=spread_pips)
        where = f"{strategy.strategy_id} {strategy.symbol} at {t.isoformat()}"

        assert isinstance(decision, StrategyDecision), where
        assert decision.strategy_id == strategy.strategy_id, where
        assert isinstance(decision.regime, RegimeOutput), where
        assert decision.signal is None or isinstance(decision.signal, TechnicalSignal), where
        assert isinstance(decision.regime_passed, bool), where
        assert decision.regime_reason_code and decision.reason_code, where
        assert isinstance(decision.regime_details, str) and isinstance(decision.details, str), where
        if not decision.regime_passed:
            assert decision.signal is None, where
            assert decision.reason_code == TECH_SKIPPED_REGIME_REJECTED, where

        assert fingerprint(again) == fingerprint(decision), f"not deterministic: {where}"
        assert fingerprint(truncated) == fingerprint(decision), f"reads bars not closed by t: {where}"
        assert requested, f"no bars fetched: {where}"
        assert max(requested) <= LIVE_BAR_EXPORT_LIMIT, f"fetch of {max(requested)} bars > export limit: {where}"

        signal = decision.signal
        if signal is not None:
            assert signal.symbol == strategy.symbol and signal.direction in {"BUY", "SELL"}, where
            assert signal.order_type == "MARKET", where
            assert signal.stop_pips > 0 and signal.take_profit_pips > 0, where
            assert signal.risk_reward >= min_rr, where
            assert signal.take_profit_pips >= min_rr * signal.stop_pips - 0.02, where
            assert signal.reason_code == decision.reason_code, where
        decisions.append(decision)
    return decisions


def session_decision_times(frames: Frames, symbol: str, *, last: int = 24, step: int = 1) -> list[pd.Timestamp]:
    """The last ``last`` in-session M15 decision times (bar closes) of ``symbol``, every ``step``-th."""
    from core.filters.session_filter import is_tradeable_session

    closes = frames[symbol]["M15"].index + pd.Timedelta(minutes=15)
    in_session = [t for t in closes if is_tradeable_session(t.to_pydatetime())]
    return in_session[-last * step::step]
