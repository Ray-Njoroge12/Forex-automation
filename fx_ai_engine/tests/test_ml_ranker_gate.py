"""Tests for the ML ranker gate in the decision pipeline."""
from __future__ import annotations

from ml.signal_ranker import PREDICT_THRESHOLD, SignalRanker


def test_ranker_returns_neutral_when_no_model() -> None:
    """With no model loaded, predict_proba returns 0.5 (neutral)."""
    ranker = SignalRanker()
    assert not ranker.is_ready()
    prob = ranker.predict_proba({"regime_confidence": 0.8, "rsi": 55.0})
    assert prob == 0.5


def test_predict_threshold_is_above_neutral() -> None:
    """PREDICT_THRESHOLD > 0.5: untrained ranker (returns 0.5) will block signals."""
    assert PREDICT_THRESHOLD > 0.5
    assert PREDICT_THRESHOLD <= 1.0


def test_ranker_load_returns_false_when_no_model_file() -> None:
    """load() returns False gracefully when no model file exists."""
    ranker = SignalRanker()
    result = ranker.load()
    assert result is False
    assert not ranker.is_ready()


def test_ranker_predict_proba_with_partial_features() -> None:
    """predict_proba handles missing feature keys gracefully."""
    ranker = SignalRanker()
    prob = ranker.predict_proba({"regime_confidence": 0.9})
    assert prob == 0.5


def test_ranker_predict_proba_with_empty_features() -> None:
    ranker = SignalRanker()
    prob = ranker.predict_proba({})
    assert prob == 0.5


def test_ranker_is_ready_false_before_load() -> None:
    ranker = SignalRanker()
    assert ranker.is_ready() is False
