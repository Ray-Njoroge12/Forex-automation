"""Tests for validation.py — walk-forward folds, PurgedKFold, metrics."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.meta_labeler.validation import (
    PurgedKFold,
    WalkForwardFold,
    cv_sharpe,
    generate_walk_forward_folds,
    precision_at_threshold,
    recall_at_threshold,
)


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def make_index(n: int, start: str = "2022-01-01", freq: str = "4h") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq=freq, tz="UTC")


# ══════════════════════════════════════════════════════════════════════
# generate_walk_forward_folds
# ══════════════════════════════════════════════════════════════════════

class TestWalkForwardFolds:
    def test_produces_correct_number_of_folds(self):
        # 36 months × 30d/mo × 6 bars/day ≈ 6480 bars @ 4h
        idx = make_index(6480, start="2022-01-01", freq="4h")
        folds = generate_walk_forward_folds(
            idx, train_months=24, test_months=3, step_months=1,
        )
        # 36 months - 24 train - 3 test = 9 possible fold start positions
        # rough expectation: ~9-10 folds
        assert 6 <= len(folds) <= 12

    def test_train_test_no_overlap(self):
        idx = make_index(4000, start="2022-01-01", freq="4h")
        folds = generate_walk_forward_folds(
            idx, train_months=12, test_months=1, step_months=1,
        )
        for fold in folds:
            assert fold.train_end <= fold.test_start, (
                f"Fold {fold.fold_index}: train ends {fold.train_end}, "
                f"test starts {fold.test_start}"
            )
            # Indices cannot overlap
            overlap = set(fold.train_indices) & set(fold.test_indices)
            assert overlap == set()

    def test_purge_enforced(self):
        idx = make_index(4000, start="2022-01-01", freq="4h")
        folds = generate_walk_forward_folds(
            idx,
            train_months=12, test_months=1, step_months=1,
            purge=pd.Timedelta(days=5),
        )
        for fold in folds:
            train_ts = idx[fold.train_indices]
            # Effective train end = train_end - 5 days
            cutoff = fold.train_end - pd.Timedelta(days=5)
            assert all(t < cutoff for t in train_ts), (
                f"Fold {fold.fold_index}: training samples past purge cutoff"
            )

    def test_embargo_enforced(self):
        idx = make_index(4000, start="2022-01-01", freq="4h")
        folds = generate_walk_forward_folds(
            idx,
            train_months=12, test_months=1, step_months=1,
            embargo=pd.Timedelta(days=3),
        )
        for fold in folds:
            # test_start must be >= train_end + embargo
            assert fold.test_start >= fold.train_end + pd.Timedelta(days=3)

    def test_fold_indices_sorted_ascending(self):
        idx = make_index(4000, start="2022-01-01", freq="4h")
        folds = generate_walk_forward_folds(
            idx, train_months=12, test_months=1, step_months=1,
        )
        for fold in folds:
            # Indices themselves should be sorted (they're positions)
            assert np.all(np.diff(fold.train_indices) > 0)
            assert np.all(np.diff(fold.test_indices) > 0)

    def test_fold_indices_are_consecutive_in_test(self):
        """Test indices should correspond to a contiguous time slice."""
        idx = make_index(4000, start="2022-01-01", freq="4h")
        folds = generate_walk_forward_folds(
            idx, train_months=12, test_months=1, step_months=1,
        )
        for fold in folds:
            # Test indices are consecutive positions
            diffs = np.diff(fold.test_indices)
            assert all(d == 1 for d in diffs), "test indices not consecutive"

    def test_unsorted_index_raises(self):
        unsorted = pd.DatetimeIndex(
            ["2024-01-03", "2024-01-01", "2024-01-02"], tz="UTC",
        )
        with pytest.raises(ValueError, match="sorted ascending"):
            generate_walk_forward_folds(unsorted)

    def test_non_datetime_index_raises(self):
        with pytest.raises(TypeError, match="DatetimeIndex"):
            generate_walk_forward_folds(np.array([1, 2, 3]))

    def test_too_short_range_raises(self):
        # 1 month of data can't satisfy 24-month training
        idx = make_index(180, start="2024-01-01", freq="4h")
        with pytest.raises(ValueError, match="No folds produced"):
            generate_walk_forward_folds(
                idx, train_months=24, test_months=3,
            )


# ══════════════════════════════════════════════════════════════════════
# PurgedKFold
# ══════════════════════════════════════════════════════════════════════

class TestPurgedKFold:
    def test_produces_n_splits(self):
        idx = make_index(1000, start="2022-01-01", freq="1h")
        pkf = PurgedKFold(n_splits=5, purge=pd.Timedelta(hours=10))
        splits = list(pkf.split(idx))
        assert len(splits) == 5

    def test_train_and_test_disjoint(self):
        idx = make_index(1000, start="2022-01-01", freq="1h")
        pkf = PurgedKFold(n_splits=5, purge=pd.Timedelta(hours=10))
        for train_idx, test_idx in pkf.split(idx):
            overlap = set(train_idx) & set(test_idx)
            assert overlap == set()

    def test_purge_respected(self):
        idx = make_index(500, start="2022-01-01", freq="1h")
        purge = pd.Timedelta(hours=24)
        pkf = PurgedKFold(n_splits=5, purge=purge)
        for train_idx, test_idx in pkf.split(idx):
            if len(train_idx) == 0 or len(test_idx) == 0:
                continue
            test_times = idx[test_idx]
            train_times = idx[train_idx]
            # No train sample inside [test_start - purge, test_end + purge]
            t_start, t_end = test_times.min(), test_times.max()
            for tr in train_times:
                assert tr <= (t_start - purge) or tr >= (t_end + purge), (
                    f"Training sample {tr} inside purge window "
                    f"[{t_start - purge}, {t_end + purge}]"
                )

    def test_covers_all_test_indices_across_folds(self):
        idx = make_index(500, start="2022-01-01", freq="1h")
        pkf = PurgedKFold(n_splits=5, purge=pd.Timedelta(0))
        all_test = set()
        for _, test_idx in pkf.split(idx):
            all_test.update(test_idx)
        # Every index should appear in exactly one test fold
        assert all_test == set(range(len(idx)))

    def test_invalid_nsplits(self):
        with pytest.raises(ValueError, match="n_splits must be"):
            PurgedKFold(n_splits=1)

    def test_n_samples_too_small(self):
        idx = make_index(3, start="2022-01-01", freq="1h")
        pkf = PurgedKFold(n_splits=5)
        with pytest.raises(ValueError, match="at least"):
            list(pkf.split(idx))


# ══════════════════════════════════════════════════════════════════════
# Strategy metric: cv_sharpe
# ══════════════════════════════════════════════════════════════════════

class TestCVSharpe:
    def test_returns_sentinel_when_few_trades_approved(self):
        y = np.array([1] * 100)
        proba = np.zeros(100)  # nothing approved at threshold 0.55
        r = np.ones(100) * 2.0
        s = cv_sharpe(y, proba, r, threshold=0.55, min_trades=10)
        assert s == -99.0

    def test_positive_on_winning_strategy(self):
        """All approved trades win → positive Sharpe."""
        y = np.array([1] * 50)
        proba = np.array([0.8] * 50)  # all approved
        r = np.ones(50) * 2.0  # all +2R
        # With constant returns, std=0 → returns 0.0 not positive
        # Use slight variance
        r_noisy = 2.0 + np.random.default_rng(0).normal(0, 0.05, size=50)
        s = cv_sharpe(y, proba, r_noisy, threshold=0.55, min_trades=10)
        assert s > 10.0  # very high sharpe on near-constant wins

    def test_negative_on_losing_strategy(self):
        y = np.array([0] * 50)
        proba = np.array([0.8] * 50)
        r = -1.0 + np.random.default_rng(0).normal(0, 0.05, size=50)
        s = cv_sharpe(y, proba, r, threshold=0.55, min_trades=10)
        assert s < -10.0

    def test_zero_on_constant_return(self):
        """Constant return → std=0 → returns 0.0."""
        y = np.ones(50)
        proba = np.ones(50) * 0.8
        r = np.ones(50) * 1.5  # constant
        assert cv_sharpe(y, proba, r, threshold=0.55, min_trades=10) == 0.0

    def test_length_mismatch_raises(self):
        y = np.array([1, 0])
        proba = np.array([0.8])  # wrong length
        r = np.array([2.0, -1.0])
        with pytest.raises(ValueError, match="Length mismatch"):
            cv_sharpe(y, proba, r)

    def test_empty_returns_sentinel(self):
        assert cv_sharpe(np.array([]), np.array([]), np.array([])) == -99.0


class TestPrecisionRecall:
    def test_precision_all_wins_approved(self):
        y = np.array([1, 1, 1, 0, 0])
        proba = np.array([0.8, 0.8, 0.8, 0.3, 0.3])
        # 3 approved (all wins) → precision = 1.0
        assert precision_at_threshold(y, proba, threshold=0.5) == 1.0

    def test_precision_nan_if_none_approved(self):
        y = np.array([1, 0])
        proba = np.array([0.3, 0.2])
        p = precision_at_threshold(y, proba, threshold=0.5)
        assert np.isnan(p)

    def test_recall_covers_wins(self):
        y = np.array([1, 1, 0, 0])
        proba = np.array([0.8, 0.3, 0.9, 0.2])
        # Approved: indices 0, 2. True wins: indices 0, 1. TP=1, recall=1/2=0.5
        assert recall_at_threshold(y, proba, threshold=0.5) == 0.5

    def test_recall_nan_if_no_wins(self):
        y = np.array([0, 0, 0])
        proba = np.array([0.9, 0.9, 0.9])
        assert np.isnan(recall_at_threshold(y, proba, threshold=0.5))
