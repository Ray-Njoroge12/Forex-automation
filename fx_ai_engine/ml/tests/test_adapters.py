"""Tests for meta-labeler adapter scaffolding."""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.types import RegimeOutput as CoreRegimeOutput
from core.types import TechnicalSignal as CoreTechnicalSignal
from ml.meta_labeler.adapters import (
    CoreRegimeAgentAdapter,
    CoreTechnicalAgentAdapter,
    HistoricalOhlcProvider,
    build_core_agent_replay_adapters,
    map_engine_regime_to_replay,
    normalize_ohlcv_columns,
    normalize_regime_label_for_features,
)
from ml.meta_labeler.signal_replay import RegimeOutput


def _make_bars(
    n: int,
    *,
    start: str,
    freq: str,
    use_tick_volume: bool = True,
) -> pd.DataFrame:
    index = pd.date_range(start=start, periods=n, freq=freq, tz="UTC")
    close = 1.1000 + np.linspace(0.0, 0.0020, n)
    high = close + 0.0005
    low = close - 0.0005
    open_ = np.concatenate(([close[0]], close[:-1]))

    data: dict[str, object] = {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "spread": np.full(n, 12, dtype="int32"),
    }
    if use_tick_volume:
        data["tick_volume"] = np.full(n, 1000, dtype="int64")
    else:
        data["volume"] = np.full(n, 1000, dtype="int64")

    return pd.DataFrame(data, index=index)


def test_normalize_ohlcv_columns_renames_tick_volume() -> None:
    df = _make_bars(5, start="2024-01-01 00:00", freq="15min", use_tick_volume=True)
    out = normalize_ohlcv_columns(df)
    assert "volume" in out.columns
    assert "tick_volume" not in out.columns


def test_map_engine_regime_to_replay_maps_trending_labels() -> None:
    assert map_engine_regime_to_replay("TRENDING_BULL") == "TREND_UP"
    assert map_engine_regime_to_replay("TRENDING_BEAR") == "TREND_DOWN"
    assert map_engine_regime_to_replay("UNKNOWN_LABEL") == "NO_REGIME"


def test_normalize_regime_label_for_features_handles_engine_labels() -> None:
    assert normalize_regime_label_for_features("TRENDING_BULL") == "TREND_UP"
    assert normalize_regime_label_for_features("TRENDING_BEAR") == "TREND_DOWN"
    assert normalize_regime_label_for_features("NO_TRADE") == "UNKNOWN"


def test_historical_provider_respects_cutoff_and_tail() -> None:
    m15 = _make_bars(100, start="2024-01-01 00:00", freq="15min")
    h1 = _make_bars(300, start="2023-12-01 00:00", freq="1h")
    provider = HistoricalOhlcProvider(symbol="EURUSD", m15_df=m15, h1_df=h1)

    cutoff = m15.index[50]
    provider.set_cutoff(cutoff)
    out = provider.fetch("EURUSD", 15, num_candles=10)

    assert len(out) == 10
    assert out.index.max() <= cutoff


def test_core_regime_adapter_maps_to_replay_protocol() -> None:
    class DummyRegimeAgent:
        def evaluate(self, _timeframe_h1: int) -> CoreRegimeOutput:
            return CoreRegimeOutput(
                regime="TRENDING_BULL",
                trend_state="BULLISH",
                volatility_state="NORMAL",
                confidence=0.9,
                reason_code="TEST",
                timestamp_utc="2024-01-01T00:00:00+00:00",
                atr_ratio=1.1,
            )

    m15 = _make_bars(100, start="2024-01-01 00:00", freq="15min")
    h1 = _make_bars(300, start="2023-12-01 00:00", freq="1h")
    provider = HistoricalOhlcProvider(symbol="EURUSD", m15_df=m15, h1_df=h1)
    adapter = CoreRegimeAgentAdapter(DummyRegimeAgent(), provider)

    out = adapter.classify(h1.iloc[:250])
    assert out.regime == "TREND_UP"
    assert 0.0 <= out.confidence <= 1.0
    assert out.extra is not None
    assert out.extra["engine_regime"] == "TRENDING_BULL"


def test_core_technical_adapter_converts_pips_to_prices() -> None:
    class DummyTechnicalAgent:
        def evaluate(
            self,
            _regime: CoreRegimeOutput,
            _timeframe_m15: int,
            _timeframe_h1: int,
            _timeframe_h4: int,
        ) -> CoreTechnicalSignal:
            return CoreTechnicalSignal(
                trade_id="t-1",
                symbol="EURUSD",
                direction="BUY",
                stop_pips=10.0,
                take_profit_pips=22.0,
                risk_reward=2.2,
                confidence=0.8,
                reason_code="DUMMY_RULE",
                timestamp_utc="2024-01-01T00:00:00+00:00",
            )

    m15 = _make_bars(120, start="2024-01-01 00:00", freq="15min")
    h1 = _make_bars(300, start="2023-12-01 00:00", freq="1h")
    provider = HistoricalOhlcProvider(symbol="EURUSD", m15_df=m15, h1_df=h1)
    adapter = CoreTechnicalAgentAdapter(DummyTechnicalAgent(), provider)

    replay_regime = RegimeOutput(regime="TREND_UP", confidence=0.8)
    out = adapter.evaluate(m15.iloc[:100], replay_regime, "EURUSD")

    assert out is not None
    assert out.direction == "BUY"
    assert out.stop_loss < out.entry < out.take_profit
    assert out.atr > 0

    stop_distance = out.entry - out.stop_loss
    tp_distance = out.take_profit - out.entry
    assert np.isclose(stop_distance, 0.001)
    assert np.isclose(tp_distance, 0.0022)


def test_factory_builds_core_agent_adapters() -> None:
    m15 = _make_bars(400, start="2024-01-01 00:00", freq="15min")
    h1 = _make_bars(800, start="2023-09-01 00:00", freq="1h")

    regime_adapter, technical_adapter, history = build_core_agent_replay_adapters(
        "EURUSD",
        m15,
        h1,
    )

    assert isinstance(regime_adapter, CoreRegimeAgentAdapter)
    assert isinstance(technical_adapter, CoreTechnicalAgentAdapter)
    assert isinstance(history, HistoricalOhlcProvider)
