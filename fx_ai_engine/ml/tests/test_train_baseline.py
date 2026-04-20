"""Tests for train_baseline.py — logistic regression baseline."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.meta_labeler.train_baseline import (
    BaselineResult,
    MAX_SHARPE_IQR,
    MIN_MEDIAN_AUC,
    MIN_MEDIAN_SHARPE,
    train_baseline,
)


# ══════════════════════════════════════════════════════════════════════
# Synthetic data generators
# ══════════════════════════════════════════════════════════════════════

def _make_timestamps(n: int, start: str = "2022-01-01", freq: str = "4h") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq=freq, tz="UTC")


def make_learnable_dataset(
    n: int = 4000,
    n_features: int = 8,
    signal_strength: float = 2.0,
    seed: int = 42,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Generate a dataset where 2 features carry clear predictive signal.

    Features 0 and 1 push the label toward 1 when positive.
    Other features are noise.
    The label is binary; realized_r is +2.2 for wins, -1.0 for losses.
    """
    rng = np.random.default_rng(seed)
    X = rng.normal(0, 1, size=(n, n_features))
    # Signal: positive f0 + f1 → more likely a win
    logits = signal_strength * (X[:, 0] + X[:, 1]) + rng.normal(0, 1, size=n)
    probs = 1 / (1 + np.exp(-logits))
    labels = (rng.random(n) < probs).astype(int)
    # Realized R: +2.2 on wins, -1.0 on losses, with slight noise
    realized = np.where(
        labels == 1,
        2.2 + rng.normal(0, 0.1, size=n),
        -1.0 + rng.normal(0, 0.1, size=n),
    )
    cols = [f"f{i}" for i in range(n_features)]
    ts = _make_timestamps(n)
    features_df = pd.DataFrame(X, columns=cols, index=ts)
    return features_df, pd.Series(labels, index=ts), pd.Series(realized, index=ts)


def make_noise_dataset(
    n: int = 4000, n_features: int = 8, seed: int = 42,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Generate a dataset with NO learnable signal — labels random."""
    rng = np.random.default_rng(seed)
    X = rng.normal(0, 1, size=(n, n_features))
    labels = rng.integers(0, 2, size=n)
    realized = np.where(
        labels == 1,
        2.2 + rng.normal(0, 0.1, size=n),
        -1.0 + rng.normal(0, 0.1, size=n),
    )
    cols = [f"f{i}" for i in range(n_features)]
    ts = _make_timestamps(n)
    features_df = pd.DataFrame(X, columns=cols, index=ts)
    return features_df, pd.Series(labels, index=ts), pd.Series(realized, index=ts)


# ══════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════

class TestBaselineResult:
    def test_empty_result(self):
        r = BaselineResult()
        assert r.n_folds == 0
        assert not r.passes_thresholds

    def test_summary_string(self):
        r = BaselineResult(
            n_folds=5, n_total_samples=1000,
            median_sharpe=0.5, median_auc=0.55, sharpe_iqr=0.8,
            passes_thresholds=True,
        )
        s = r.summary()
        assert "PASS" in s
        assert "0.500" in s


class TestTrainBaseline:
    def test_learnable_data_passes_thresholds(self):
        """With a clear predictive signal, baseline should pass."""
        X, y, r = make_learnable_dataset(n=4000, signal_strength=2.0)
        result = train_baseline(
            X, y, r,
            train_months=12, test_months=1, step_months=1,
        )
        # Must produce folds
        assert result.n_folds > 0
        # Must pass AUC threshold — real signal is present
        assert result.median_auc >= MIN_MEDIAN_AUC, (
            f"median_auc={result.median_auc:.3f} below {MIN_MEDIAN_AUC}. "
            f"Failures: {result.failure_reasons}"
        )

    def test_noise_data_fails_thresholds(self):
        """With pure noise, baseline should clearly fail AUC."""
        X, y, r = make_noise_dataset(n=4000)
        result = train_baseline(
            X, y, r,
            train_months=12, test_months=1, step_months=1,
        )
        # Noise should not achieve median AUC ≥ 0.53
        assert result.median_auc < 0.55, (
            f"Noise dataset somehow got AUC={result.median_auc:.3f}"
        )

    def test_returns_fold_metrics(self):
        X, y, r = make_learnable_dataset(n=4000)
        result = train_baseline(
            X, y, r,
            train_months=12, test_months=1, step_months=1,
        )
        for fr in result.fold_results:
            assert fr.train_size > 0
            assert fr.test_size > 0
            # AUC is in [0, 1] or NaN
            assert np.isnan(fr.auc) or 0 <= fr.auc <= 1

    def test_label_length_mismatch_raises(self):
        X, y, r = make_learnable_dataset(n=1000)
        with pytest.raises(ValueError, match="Length mismatch"):
            train_baseline(X, y.iloc[:500], r)

    def test_non_binary_labels_raises(self):
        X, y, r = make_learnable_dataset(n=1000)
        y2 = y.copy()
        y2.iloc[0] = 2  # non-binary
        with pytest.raises(ValueError, match="labels must be binary"):
            train_baseline(X, y2, r)

    def test_non_datetime_index_raises(self):
        X, y, r = make_learnable_dataset(n=1000)
        X2 = X.reset_index(drop=True)
        y2 = y.reset_index(drop=True)
        r2 = r.reset_index(drop=True)
        with pytest.raises(TypeError, match="DatetimeIndex"):
            train_baseline(X2, y2, r2)

    def test_aggregate_has_passes_thresholds_flag(self):
        X, y, r = make_learnable_dataset(n=4000)
        result = train_baseline(
            X, y, r,
            train_months=12, test_months=1, step_months=1,
        )
        assert isinstance(result.passes_thresholds, bool)
        # If passing, no failure reasons
        if result.passes_thresholds:
            assert result.failure_reasons == []
        else:
            assert len(result.failure_reasons) > 0
