"""Tests for label.py — triple-barrier labeling."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.meta_labeler.label import (
    DEFAULT_TTL_BARS_M15,
    LabelOutcome,
    LabeledCandidate,
    label_all,
    label_summary,
    labeled_to_dataframe,
    triple_barrier_label,
)
from ml.meta_labeler.signal_replay import CandidateSignal


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def make_candidate(
    direction: str = "BUY",
    entry: float = 1.1000,
    stop_loss: float = 1.0980,
    take_profit: float = 1.1044,  # 2.2:1 R:R
    timestamp: str = "2024-01-01 10:00",
) -> CandidateSignal:
    return CandidateSignal(
        timestamp=pd.Timestamp(timestamp, tz="UTC"),
        symbol="EURUSD",
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr=0.0020,
        regime="TREND_UP" if direction == "BUY" else "TREND_DOWN",
        regime_confidence=0.8,
        rule_name="test",
        risk_reward=2.2,
    )


def make_future_bars(
    highs: list[float],
    lows: list[float],
    closes: list[float] | None = None,
    start: str = "2024-01-01 10:15",
    freq: str = "15min",
) -> pd.DataFrame:
    n = len(highs)
    if closes is None:
        closes = [(h + l) / 2 for h, l in zip(highs, lows)]
    return pd.DataFrame(
        {"high": highs, "low": lows, "close": closes, "open": closes},
        index=pd.date_range(start, periods=n, freq=freq, tz="UTC"),
    )


# ══════════════════════════════════════════════════════════════════════
# LabelOutcome
# ══════════════════════════════════════════════════════════════════════

class TestLabelOutcome:
    def test_values(self):
        assert LabelOutcome.LOSS == 0
        assert LabelOutcome.WIN == 1
        assert LabelOutcome.TIMEOUT == 2

    def test_is_win(self):
        assert LabelOutcome.WIN.is_win
        assert not LabelOutcome.LOSS.is_win
        assert not LabelOutcome.TIMEOUT.is_win


# ══════════════════════════════════════════════════════════════════════
# BUY direction — known outcomes
# ══════════════════════════════════════════════════════════════════════

class TestBuyLabeling:
    def test_immediate_win(self):
        """First bar after entry spikes to TP → WIN in 1 bar."""
        cand = make_candidate(direction="BUY", entry=1.1000,
                               stop_loss=1.0980, take_profit=1.1044)
        future = make_future_bars(
            highs=[1.1050, 1.1060],  # bar 1 high exceeds TP
            lows=[1.0999, 1.1020],
        )
        result = triple_barrier_label(cand, future, ttl_bars=5)
        assert result.outcome is LabelOutcome.WIN
        assert result.bars_held == 1
        assert result.exit_price == pytest.approx(1.1044)
        assert result.realized_r == pytest.approx(2.2)

    def test_immediate_loss(self):
        """First bar after entry collapses to SL → LOSS in 1 bar."""
        cand = make_candidate(direction="BUY", entry=1.1000,
                               stop_loss=1.0980, take_profit=1.1044)
        future = make_future_bars(
            highs=[1.1005, 1.1010],
            lows=[1.0970, 1.0990],  # bar 1 low breaches SL
        )
        result = triple_barrier_label(cand, future, ttl_bars=5)
        assert result.outcome is LabelOutcome.LOSS
        assert result.bars_held == 1
        assert result.exit_price == pytest.approx(1.0980)
        assert result.realized_r == pytest.approx(-1.0)

    def test_same_bar_both_hit_is_loss(self):
        """Conservative: same-bar SL+TP defaults to LOSS."""
        cand = make_candidate(direction="BUY", entry=1.1000,
                               stop_loss=1.0980, take_profit=1.1044)
        future = make_future_bars(
            highs=[1.1050],  # exceeds TP
            lows=[1.0970],   # breaches SL — same bar
        )
        result = triple_barrier_label(cand, future, ttl_bars=5)
        assert result.outcome is LabelOutcome.LOSS
        assert result.realized_r == pytest.approx(-1.0)

    def test_timeout_positive_pnl(self):
        """Neither barrier hit within TTL → TIMEOUT, exit at last close."""
        cand = make_candidate(direction="BUY", entry=1.1000,
                               stop_loss=1.0980, take_profit=1.1044)
        # Narrow range, neither barrier hit, final close slightly up
        future = make_future_bars(
            highs=[1.1010, 1.1015, 1.1020],
            lows=[1.0990, 1.0995, 1.0990],
            closes=[1.1000, 1.1010, 1.1010],
        )
        result = triple_barrier_label(cand, future, ttl_bars=3)
        assert result.outcome is LabelOutcome.TIMEOUT
        assert result.bars_held == 3
        # realized_r = +10 pips / 20 pips risk = +0.5
        assert result.realized_r == pytest.approx(0.5)

    def test_timeout_negative_pnl(self):
        cand = make_candidate(direction="BUY", entry=1.1000,
                               stop_loss=1.0980, take_profit=1.1044)
        future = make_future_bars(
            highs=[1.1010, 1.1005, 1.1000],
            lows=[1.0985, 1.0990, 1.0990],
            closes=[1.0995, 1.0995, 1.0995],
        )
        result = triple_barrier_label(cand, future, ttl_bars=3)
        assert result.outcome is LabelOutcome.TIMEOUT
        # -5 pips / 20 pips = -0.25
        assert result.realized_r == pytest.approx(-0.25)

    def test_win_on_bar_3(self):
        cand = make_candidate(direction="BUY", entry=1.1000,
                               stop_loss=1.0980, take_profit=1.1044)
        future = make_future_bars(
            highs=[1.1010, 1.1020, 1.1050, 1.1060],
            lows=[1.0995, 1.0990, 1.1010, 1.1040],
        )
        result = triple_barrier_label(cand, future, ttl_bars=5)
        assert result.outcome is LabelOutcome.WIN
        assert result.bars_held == 3


# ══════════════════════════════════════════════════════════════════════
# SELL direction — symmetric
# ══════════════════════════════════════════════════════════════════════

class TestSellLabeling:
    def test_immediate_win(self):
        """SELL wins when price drops to TP."""
        cand = make_candidate(direction="SELL", entry=1.1000,
                               stop_loss=1.1020, take_profit=1.0956)
        future = make_future_bars(
            highs=[1.1010],
            lows=[1.0950],  # breaches TP (down = profit for SELL)
        )
        result = triple_barrier_label(cand, future, ttl_bars=5)
        assert result.outcome is LabelOutcome.WIN
        assert result.bars_held == 1
        assert result.exit_price == pytest.approx(1.0956)
        assert result.realized_r > 2.0

    def test_immediate_loss(self):
        """SELL loses when price rises to SL."""
        cand = make_candidate(direction="SELL", entry=1.1000,
                               stop_loss=1.1020, take_profit=1.0956)
        future = make_future_bars(
            highs=[1.1030],  # breaches SL (up = loss for SELL)
            lows=[1.1010],
        )
        result = triple_barrier_label(cand, future, ttl_bars=5)
        assert result.outcome is LabelOutcome.LOSS
        assert result.realized_r == pytest.approx(-1.0)

    def test_same_bar_sell_both_hit_is_loss(self):
        cand = make_candidate(direction="SELL", entry=1.1000,
                               stop_loss=1.1020, take_profit=1.0956)
        future = make_future_bars(
            highs=[1.1030],  # hits SL
            lows=[1.0950],   # hits TP — same bar
        )
        result = triple_barrier_label(cand, future, ttl_bars=5)
        assert result.outcome is LabelOutcome.LOSS


# ══════════════════════════════════════════════════════════════════════
# Validation errors
# ══════════════════════════════════════════════════════════════════════

class TestValidation:
    def test_empty_future_raises(self):
        cand = make_candidate()
        empty = pd.DataFrame(
            {"high": [], "low": [], "close": []},
            index=pd.DatetimeIndex([], tz="UTC"),
        )
        with pytest.raises(ValueError, match="future_bars is empty"):
            triple_barrier_label(cand, empty, ttl_bars=5)

    def test_missing_columns_raises(self):
        cand = make_candidate()
        bad = pd.DataFrame({"close": [1.1]})
        with pytest.raises(ValueError, match="high, low, close"):
            triple_barrier_label(cand, bad, ttl_bars=5)

    def test_zero_risk_raises(self):
        """Candidate with entry == stop_loss has zero risk — must reject."""
        # Manually build invalid candidate bypassing normal validation
        cand = CandidateSignal(
            timestamp=pd.Timestamp("2024-01-01", tz="UTC"),
            symbol="EURUSD", direction="BUY",
            entry=1.10, stop_loss=1.10, take_profit=1.12,
            atr=0.01, regime="TREND_UP", regime_confidence=0.8,
        )
        future = make_future_bars(highs=[1.15], lows=[1.09])
        with pytest.raises(ValueError, match="zero risk"):
            triple_barrier_label(cand, future, ttl_bars=5)

    def test_invalid_ttl_raises(self):
        cand = make_candidate()
        future = make_future_bars(highs=[1.11], lows=[1.09])
        with pytest.raises(ValueError, match="ttl_bars must be"):
            triple_barrier_label(cand, future, ttl_bars=0)


# ══════════════════════════════════════════════════════════════════════
# Batch labeling
# ══════════════════════════════════════════════════════════════════════

class TestLabelAll:
    def _make_bars(self, n: int, start: str, freq: str = "15min") -> pd.DataFrame:
        idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
        # Slowly rising price: each bar 2 pips higher
        closes = 1.1000 + np.arange(n) * 0.0002
        return pd.DataFrame(
            {
                "open": closes,
                "high": closes + 0.0005,
                "low": closes - 0.0005,
                "close": closes,
            },
            index=idx,
        )

    def test_skips_candidates_near_end(self):
        """Candidates in the last TTL bars should be skipped."""
        bars = self._make_bars(100, "2024-01-01 00:00")
        candidates = [
            # early — labelable
            make_candidate(timestamp="2024-01-01 00:15"),
            # near end — insufficient lookahead
            make_candidate(timestamp=str(bars.index[-2])),
        ]
        labeled = label_all(candidates, bars, ttl_bars=24)
        # Only the early candidate should be labeled
        assert len(labeled) == 1
        assert labeled[0].candidate.timestamp == pd.Timestamp("2024-01-01 00:15", tz="UTC")

    def test_labels_all_valid_candidates(self):
        bars = self._make_bars(200, "2024-01-01 00:00")
        candidates = [
            make_candidate(timestamp="2024-01-01 00:15"),
            make_candidate(timestamp="2024-01-01 01:00"),
            make_candidate(timestamp="2024-01-01 02:00"),
        ]
        labeled = label_all(candidates, bars, ttl_bars=24)
        assert len(labeled) == 3

    def test_preserves_order(self):
        bars = self._make_bars(500, "2024-01-01 00:00")
        timestamps = [
            "2024-01-01 00:30",
            "2024-01-01 02:00",
            "2024-01-01 01:00",
        ]
        candidates = [make_candidate(timestamp=t) for t in timestamps]
        labeled = label_all(candidates, bars, ttl_bars=24)
        # Output order matches input order
        assert [lc.candidate.timestamp for lc in labeled] == [
            pd.Timestamp(t, tz="UTC") for t in timestamps
        ]


# ══════════════════════════════════════════════════════════════════════
# Conversions and summaries
# ══════════════════════════════════════════════════════════════════════

class TestLabeledToDataFrame:
    def test_empty(self):
        df = labeled_to_dataframe([])
        assert df.empty

    def test_populated(self):
        cand = make_candidate()
        lc = LabeledCandidate(
            candidate=cand,
            outcome=LabelOutcome.WIN,
            bars_held=5,
            exit_price=1.1044,
            exit_time=pd.Timestamp("2024-01-01 11:15", tz="UTC"),
            realized_r=2.2,
        )
        df = labeled_to_dataframe([lc])
        assert len(df) == 1
        assert df["outcome"].iloc[0] == "WIN"
        assert df["binary_label"].iloc[0] == 1
        assert df["realized_r"].iloc[0] == pytest.approx(2.2)


class TestLabelSummary:
    def test_empty_summary(self):
        s = label_summary([])
        assert s["total"] == 0
        assert s["win_rate"] == 0.0

    def test_populated_summary(self):
        cand = make_candidate()
        labeled = [
            LabeledCandidate(cand, LabelOutcome.WIN, 5, 1.1, pd.Timestamp("2024-01-01 11:15", tz="UTC"), 2.0),
            LabeledCandidate(cand, LabelOutcome.WIN, 3, 1.1, pd.Timestamp("2024-01-01 11:15", tz="UTC"), 2.2),
            LabeledCandidate(cand, LabelOutcome.LOSS, 10, 1.09, pd.Timestamp("2024-01-01 12:00", tz="UTC"), -1.0),
            LabeledCandidate(cand, LabelOutcome.TIMEOUT, 24, 1.10, pd.Timestamp("2024-01-01 16:00", tz="UTC"), 0.1),
        ]
        s = label_summary(labeled)
        assert s["total"] == 4
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert s["timeouts"] == 1
        assert s["win_rate"] == pytest.approx(0.5)
        assert s["binary_positive_rate"] == pytest.approx(0.5)
