"""Tests for the ML ranker gate in the decision pipeline."""
from __future__ import annotations

import pytest

joblib = pytest.importorskip("joblib")

from ml import signal_ranker as ranker_mod
from ml.signal_ranker import (
    MIN_TRAINING_SAMPLES,
    MODEL_METADATA_VERSION,
    PREDICT_THRESHOLD,
    SignalRanker,
)


class _SerializableModel:
    def predict_proba(self, _features):
        return [[0.2, 0.8]]


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


def test_ranker_rejects_legacy_model_without_metadata(tmp_path, monkeypatch) -> None:
    model_path = tmp_path / "signal_ranker_model.joblib"
    joblib.dump({"not": "a model bundle"}, model_path)
    monkeypatch.setattr(ranker_mod, "MODEL_PATH", model_path)

    ranker = SignalRanker()
    assert ranker.load() is False
    assert ranker.is_ready() is False


def test_ranker_loads_clean_scoped_model_bundle(tmp_path, monkeypatch) -> None:
    model_path = tmp_path / "signal_ranker_model.joblib"
    joblib.dump(
        {
            "model": _SerializableModel(),
            "metadata": {
                "metadata_version": MODEL_METADATA_VERSION,
                "policy_mode": ranker_mod.TRAINING_POLICY_MODE,
                "execution_mode": ranker_mod.TRAINING_EXECUTION_MODE,
                "sample_count": MIN_TRAINING_SAMPLES,
            },
        },
        model_path,
    )
    monkeypatch.setattr(ranker_mod, "MODEL_PATH", model_path)

    ranker = SignalRanker()
    assert ranker.load() is True
    assert ranker.is_ready() is True
    assert ranker.predict_proba({}) == 0.8
