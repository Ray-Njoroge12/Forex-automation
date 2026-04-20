"""Tests for signal_replay.py — historical candidate generation."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.meta_labeler.signal_replay import (
    CandidateSignal,
    ConstantRegimeAgent,
    RegimeOutput,
    SimpleEMACrossTechnicalAgent,
    TechnicalSignal,
    candidates_to_dataframe,
    replay_signals,
)


# ══════════════════════════════════════════════════════════════════════
# Data contract tests
# ══════════════════════════════════════════════════════════════════════

class TestRegimeOutput:
    def test_valid_values(self):
        out = RegimeOutput(regime="TREND_UP", confidence=0.8)
        assert out.regime == "TREND_UP"
        assert out.confidence == 0.8

    def test_unknown_regime_coerced_to_no_regime(self):
        out = RegimeOutput(regime="WEIRD_LABEL", confidence=0.5)
        assert out.regime == "NO_REGIME"

    def test_invalid_confidence_raises(self):
        with pytest.raises(ValueError):
            RegimeOutput(regime="TREND_UP", confidence=1.5)
        with pytest.raises(ValueError):
            RegimeOutput(regime="TREND_UP", confidence=-0.1)


class TestTechnicalSignal:
    def test_valid_buy(self):
        s = TechnicalSignal(
            direction="BUY", entry=1.10, stop_loss=1.09,
            take_profit=1.12, atr=0.01,
        )
        assert s.direction == "BUY"
        assert s.risk_reward == pytest.approx(2.0)

    def test_valid_sell(self):
        s = TechnicalSignal(
            direction="SELL", entry=1.10, stop_loss=1.11,
            take_profit=1.08, atr=0.01,
        )
        assert s.direction == "SELL"
        assert s.risk_reward == pytest.approx(2.0)

    def test_invalid_direction(self):
        with pytest.raises(ValueError, match="direction must be"):
            TechnicalSignal(
                direction="HOLD", entry=1.10, stop_loss=1.09,
                take_profit=1.12, atr=0.01,
            )

    def test_buy_with_invalid_sl_raises(self):
        # BUY requires sl < entry
        with pytest.raises(ValueError, match="BUY signal"):
            TechnicalSignal(
                direction="BUY", entry=1.10, stop_loss=1.11,
                take_profit=1.12, atr=0.01,
            )

    def test_sell_with_invalid_tp_raises(self):
        with pytest.raises(ValueError, match="SELL signal"):
            TechnicalSignal(
                direction="SELL", entry=1.10, stop_loss=1.11,
                take_profit=1.12, atr=0.01,
            )

    def test_zero_atr_raises(self):
        with pytest.raises(ValueError, match="atr must be positive"):
            TechnicalSignal(
                direction="BUY", entry=1.10, stop_loss=1.09,
                take_profit=1.12, atr=0.0,
            )


class TestCandidateSignal:
    def test_to_dict_round_trip(self):
        c = CandidateSignal(
            timestamp=pd.Timestamp("2024-01-01", tz="UTC"),
            symbol="EURUSD", direction="BUY",
            entry=1.10, stop_loss=1.09, take_profit=1.12,
            atr=0.01, regime="TREND_UP", regime_confidence=0.8,
            rule_name="test_rule", risk_reward=2.0,
        )
        d = c.to_dict()
        assert d["symbol"] == "EURUSD"
        assert d["entry"] == 1.10
        assert d["risk_reward"] == 2.0


# ══════════════════════════════════════════════════════════════════════
# Stub agents behave correctly
# ══════════════════════════════════════════════════════════════════════

class TestStubAgents:
    def test_constant_regime_agent(self):
        agent = ConstantRegimeAgent(regime="TREND_UP", confidence=0.9)
        df = pd.DataFrame({"close": [1.0, 1.1, 1.2]})
        out = agent.classify(df)
        assert out.regime == "TREND_UP"
        assert out.confidence == 0.9

    def test_ema_cross_agent_fires_on_engineered_cross(self):
        """Build a bar sequence specifically designed to produce a
        bullish cross above EMA(50), then verify the stub fires."""
        # 100 flat bars around 1.1000, then a sharp rally above EMA.
        n_flat, n_up = 70, 30
        flat = np.full(n_flat, 1.1000)
        up = np.linspace(1.1000, 1.1100, n_up)  # steady climb
        close = np.concatenate([flat, up])
        high = close + 0.0005
        low = close - 0.0005
        open_ = np.concatenate(([close[0]], close[:-1]))
        idx = pd.date_range("2024-01-01", periods=len(close), freq="15min", tz="UTC")
        bars = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close,
             "volume": 1000, "spread": 10},
            index=idx,
        )

        agent = SimpleEMACrossTechnicalAgent(ema_period=50)
        regime = RegimeOutput(regime="TREND_UP", confidence=1.0)
        fired = 0
        for i in range(55, len(bars)):
            signal = agent.evaluate(bars.iloc[:i + 1], regime, "EURUSD")
            if signal is not None:
                fired += 1
                assert signal.direction == "BUY"
                assert signal.entry > signal.stop_loss
                assert signal.entry < signal.take_profit
                assert signal.atr > 0
        assert fired > 0, "EMA cross agent never fired on engineered cross data"


# ══════════════════════════════════════════════════════════════════════
# The replay loop
# ══════════════════════════════════════════════════════════════════════

class TestReplaySignals:
    def test_warmup_skips_early_bars(self, m15_bars, h1_bars):
        regime = ConstantRegimeAgent("TREND_UP")
        tech = SimpleEMACrossTechnicalAgent(ema_period=50)
        candidates = replay_signals(
            m15_bars, h1_bars, "EURUSD", regime, tech, m15_warmup=250,
        )
        # Any candidate must have index >= 250 in the m15_bars frame
        for c in candidates:
            assert c.timestamp >= m15_bars.index[250]

    def test_no_regime_short_circuits(self, m15_bars, h1_bars):
        """When regime agent returns NO_REGIME, technical agent isn't called."""
        class NoRegimeAgent:
            def __init__(self):
                self.call_count = 0
            def classify(self, h1):
                self.call_count += 1
                return RegimeOutput(regime="NO_REGIME", confidence=0.0)

        class CountingTechnical:
            def __init__(self):
                self.call_count = 0
            def evaluate(self, m15, regime, sym):
                self.call_count += 1
                return None

        reg = NoRegimeAgent()
        tech = CountingTechnical()
        candidates = replay_signals(
            m15_bars, h1_bars, "EURUSD", reg, tech, m15_warmup=250,
        )
        assert candidates == []
        # Regime was called, technical was NOT
        assert reg.call_count > 0
        assert tech.call_count == 0

    def test_candidates_are_sorted_by_timestamp(self, m15_bars, h1_bars):
        regime = ConstantRegimeAgent("TREND")  # allow both dirs
        tech = SimpleEMACrossTechnicalAgent()
        candidates = replay_signals(
            m15_bars, h1_bars, "EURUSD", regime, tech, m15_warmup=250,
        )
        times = [c.timestamp for c in candidates]
        assert times == sorted(times)

    def test_symbol_is_propagated(self, m15_bars, h1_bars):
        regime = ConstantRegimeAgent("TREND")
        tech = SimpleEMACrossTechnicalAgent()
        candidates = replay_signals(
            m15_bars, h1_bars, "GBPUSD", regime, tech, m15_warmup=250,
        )
        for c in candidates:
            assert c.symbol == "GBPUSD"

    def test_point_in_time_no_lookahead(self, m15_bars, h1_bars):
        """Agents must receive only historical data.
        We verify by making the technical agent assert it has exactly N bars.
        """
        received_lengths: list[int] = []

        class CheckingAgent:
            def evaluate(self, m15, regime, sym):
                received_lengths.append(len(m15))
                return None

        reg = ConstantRegimeAgent("TREND_UP")
        tech = CheckingAgent()
        replay_signals(
            m15_bars, h1_bars, "EURUSD", reg, tech, m15_warmup=250,
        )
        # First call should see warmup+1 bars, last call should see all bars
        expected = list(range(251, len(m15_bars) + 1))
        assert received_lengths == expected

    def test_on_error_skip(self, m15_bars, h1_bars):
        """An exception in an agent is logged but doesn't kill the run."""
        call_count = [0]

        class FlakyAgent:
            def classify(self, h1):
                call_count[0] += 1
                if call_count[0] % 2 == 0:
                    raise RuntimeError("boom")
                return RegimeOutput(regime="NO_REGIME", confidence=0.5)

        reg = FlakyAgent()
        tech = SimpleEMACrossTechnicalAgent()
        # Should NOT raise
        candidates = replay_signals(
            m15_bars, h1_bars, "EURUSD", reg, tech,
            m15_warmup=250, on_error="skip",
        )
        assert candidates == []  # all skipped

    def test_on_error_raise(self, m15_bars, h1_bars):
        class BrokenAgent:
            def classify(self, h1):
                raise RuntimeError("always fails")

        tech = SimpleEMACrossTechnicalAgent()
        with pytest.raises(RuntimeError, match="RegimeAgent failed"):
            replay_signals(
                m15_bars, h1_bars, "EURUSD", BrokenAgent(), tech,
                m15_warmup=250, on_error="raise",
            )

    def test_invalid_index_raises(self, m15_bars):
        bad = pd.DataFrame({"close": [1.0]})  # no DatetimeIndex
        reg = ConstantRegimeAgent("TREND_UP")
        tech = SimpleEMACrossTechnicalAgent()
        with pytest.raises(TypeError, match="DatetimeIndex"):
            replay_signals(bad, m15_bars, "EURUSD", reg, tech)

    def test_warmup_larger_than_data_returns_empty(self, m15_bars, h1_bars):
        reg = ConstantRegimeAgent("TREND_UP")
        tech = SimpleEMACrossTechnicalAgent()
        result = replay_signals(
            m15_bars, h1_bars, "EURUSD", reg, tech,
            m15_warmup=len(m15_bars) + 100,
        )
        assert result == []

    def test_candidate_fields_valid(self, m15_bars, h1_bars):
        regime = ConstantRegimeAgent("TREND")
        tech = SimpleEMACrossTechnicalAgent()
        candidates = replay_signals(
            m15_bars, h1_bars, "EURUSD", regime, tech, m15_warmup=250,
        )
        if not candidates:
            pytest.skip("No candidates fired — can't verify fields")
        for c in candidates:
            assert c.direction in ("BUY", "SELL")
            assert c.atr > 0
            assert c.risk_reward > 0
            assert c.regime in ("TREND", "TREND_UP", "TREND_DOWN", "TRENDING")


# ══════════════════════════════════════════════════════════════════════
# candidates_to_dataframe
# ══════════════════════════════════════════════════════════════════════

class TestCandidatesToDataFrame:
    def test_empty_input(self):
        df = candidates_to_dataframe([])
        assert df.empty
        assert "symbol" in df.columns

    def test_populated_df(self):
        candidates = [
            CandidateSignal(
                timestamp=pd.Timestamp("2024-01-01 10:00", tz="UTC"),
                symbol="EURUSD", direction="BUY",
                entry=1.1, stop_loss=1.09, take_profit=1.12,
                atr=0.01, regime="TREND_UP", regime_confidence=0.8,
                rule_name="r1", risk_reward=2.0,
            ),
            CandidateSignal(
                timestamp=pd.Timestamp("2024-01-01 09:00", tz="UTC"),
                symbol="EURUSD", direction="SELL",
                entry=1.1, stop_loss=1.11, take_profit=1.08,
                atr=0.01, regime="TREND_DOWN", regime_confidence=0.8,
                rule_name="r2", risk_reward=2.0,
            ),
        ]
        df = candidates_to_dataframe(candidates)
        assert len(df) == 2
        # Sorted ascending
        assert df.index[0] < df.index[1]
