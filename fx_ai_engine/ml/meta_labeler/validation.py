"""Walk-forward validation harness with purge + embargo.

Why random K-fold is wrong for time-series:
    Financial samples are NOT i.i.d. Label leakage happens when:
    - The label for sample t-1 depends on bars [t-1, t+N] (forward window)
    - Sample t is in the training set, sample t-1 in the test set
    - The label's info window overlaps training → the model learns
      from future information relative to its "present."

Purge: Remove training samples whose LABEL window overlaps the test set.
Embargo: After each test fold, skip N bars before the next train set to
    prevent leakage in the OPPOSITE direction (test info leaking into
    the next train fold).

The standard protocol:
    24mo train | (purge) | 3mo test | (embargo) | next window starts

We also implement a simpler PurgedKFold class for hyperparameter search
where each "fold" is an equal slice of the dataset with temporal order
preserved.

References:
    Lopez de Prado, "Advances in Financial Machine Learning" (2018),
    chapters 4-7 on Labeling and Cross-Validation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Walk-forward folds
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WalkForwardFold:
    """A single walk-forward train/test split.

    Attributes:
        fold_index: 0-indexed position in the sequence.
        train_start: inclusive start of training window.
        train_end: exclusive end of training window (before purge).
        test_start: inclusive start of test window (after embargo).
        test_end: exclusive end of test window.
        train_indices: numpy int array of positions in the indexed array.
        test_indices: numpy int array of positions in the indexed array.
    """
    fold_index: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_indices: np.ndarray
    test_indices: np.ndarray

    @property
    def n_train(self) -> int:
        return len(self.train_indices)

    @property
    def n_test(self) -> int:
        return len(self.test_indices)

    def __repr__(self) -> str:
        return (
            f"WalkForwardFold(#{self.fold_index}: "
            f"train {self.n_train} [{self.train_start.date()}..{self.train_end.date()}] "
            f"test {self.n_test} [{self.test_start.date()}..{self.test_end.date()}])"
        )


def generate_walk_forward_folds(
    timestamps: pd.DatetimeIndex,
    train_months: int = 24,
    test_months: int = 3,
    step_months: int = 1,
    purge: Optional[pd.Timedelta] = None,
    embargo: Optional[pd.Timedelta] = None,
) -> list[WalkForwardFold]:
    """Generate walk-forward folds with purge + embargo.

    Args:
        timestamps: the temporal index of the candidate set (sorted ASC).
        train_months: length of the training window in calendar months.
        test_months: length of the test window in calendar months.
        step_months: how much to advance each fold.
        purge: timedelta to exclude from end of training window to
            prevent label-window leakage into test. Default: 1 day
            (matches M15 TTL for triple-barrier labeling).
        embargo: timedelta to skip after test window before next fold.
            Default: 1 day.

    Returns:
        list of WalkForwardFold, chronologically ordered.

    Raises:
        ValueError: if timestamps aren't sorted ASC, or range too short.
    """
    if not isinstance(timestamps, pd.DatetimeIndex):
        raise TypeError("timestamps must be a pd.DatetimeIndex")
    if len(timestamps) < 2:
        raise ValueError("Need at least 2 timestamps to generate folds")
    if not timestamps.is_monotonic_increasing:
        raise ValueError("timestamps must be sorted ascending")

    purge = purge if purge is not None else pd.Timedelta(days=1)
    embargo = embargo if embargo is not None else pd.Timedelta(days=1)

    series = timestamps.to_series()
    series_start = series.min()
    series_end = series.max()

    folds: list[WalkForwardFold] = []

    # First fold begins when train_months of history are available
    current_train_end = series_start + pd.DateOffset(months=train_months)
    fold_i = 0

    while True:
        train_start = current_train_end - pd.DateOffset(months=train_months)
        train_end = current_train_end
        test_start = train_end + embargo
        test_end = test_start + pd.DateOffset(months=test_months)

        if test_end > series_end + pd.Timedelta(seconds=1):
            # Not enough data for this fold — stop
            break

        # Apply purge: training window ends `purge` BEFORE the
        # nominal train_end so labels forming at the end of train
        # don't touch the test set.
        effective_train_end = train_end - purge

        train_mask = (series >= train_start) & (series < effective_train_end)
        test_mask = (series >= test_start) & (series < test_end)

        train_idx = np.where(train_mask.values)[0]
        test_idx = np.where(test_mask.values)[0]

        # Only keep folds with meaningful data in both halves
        if len(train_idx) == 0 or len(test_idx) == 0:
            logger.warning(
                "Skipping fold %d: empty train (%d) or test (%d)",
                fold_i, len(train_idx), len(test_idx),
            )
        else:
            folds.append(WalkForwardFold(
                fold_index=fold_i,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                train_indices=train_idx,
                test_indices=test_idx,
            ))
            fold_i += 1

        current_train_end = current_train_end + pd.DateOffset(months=step_months)

    if not folds:
        raise ValueError(
            f"No folds produced. Total range "
            f"{series_start} → {series_end} may be shorter than "
            f"train_months ({train_months}) + test_months ({test_months})."
        )

    logger.info(
        "Generated %d walk-forward folds (train=%d mo, test=%d mo, step=%d mo)",
        len(folds), train_months, test_months, step_months,
    )
    return folds


# ══════════════════════════════════════════════════════════════════════
# Purged K-Fold (for hyperparameter search)
# ══════════════════════════════════════════════════════════════════════

class PurgedKFold:
    """K-fold CV that preserves temporal ordering and purges around test set.

    Each fold's training set excludes samples within `purge` of the test
    boundaries. Used during hyperparameter tuning when walk-forward would
    be too expensive (each trial × each fold × each HP combo).

    Unlike sklearn.KFold, this does NOT shuffle and the test fold moves
    forward in time.
    """

    def __init__(
        self,
        n_splits: int = 5,
        purge: pd.Timedelta = pd.Timedelta(days=1),
    ):
        if n_splits < 2:
            raise ValueError(f"n_splits must be >= 2, got {n_splits}")
        self.n_splits = n_splits
        self.purge = purge

    def split(
        self,
        timestamps: pd.DatetimeIndex,
    ) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """Yield (train_indices, test_indices) for each fold."""
        if not isinstance(timestamps, pd.DatetimeIndex):
            raise TypeError("timestamps must be a pd.DatetimeIndex")
        if not timestamps.is_monotonic_increasing:
            raise ValueError("timestamps must be sorted ascending")
        n = len(timestamps)
        if n < self.n_splits:
            raise ValueError(
                f"Need at least {self.n_splits} samples, got {n}"
            )

        fold_size = n // self.n_splits

        for k in range(self.n_splits):
            test_start_idx = k * fold_size
            test_end_idx = (k + 1) * fold_size if k < self.n_splits - 1 else n
            test_idx = np.arange(test_start_idx, test_end_idx)

            # Build train indices: everything outside test PLUS purge buffer
            test_start_ts = timestamps[test_start_idx]
            test_end_ts = timestamps[test_end_idx - 1]

            ts_series = timestamps.to_series()
            before_purge = ts_series < (test_start_ts - self.purge)
            after_purge = ts_series > (test_end_ts + self.purge)
            train_mask = (before_purge | after_purge).values

            train_idx = np.where(train_mask)[0]

            if len(train_idx) == 0:
                logger.warning(
                    "PurgedKFold fold %d has empty training set", k,
                )
                continue

            yield train_idx, test_idx


# ══════════════════════════════════════════════════════════════════════
# Strategy-level metric: Sharpe of realized R-multiples after gating
# ══════════════════════════════════════════════════════════════════════

def cv_sharpe(
    y_true: np.ndarray,
    y_pred_proba: np.ndarray,
    realized_r: np.ndarray,
    threshold: float = 0.55,
    min_trades: int = 10,
    annualization: float = 52.0,
) -> float:
    """Compute strategy-level Sharpe of trades the model approves.

    Unlike AUC (which measures ranking quality), this measures the
    *realized* PnL Sharpe if we actually gated trades with the model.

    Args:
        y_true: binary ground truth (1=win, 0=otherwise).
        y_pred_proba: model's predicted P(win) per sample.
        realized_r: realized R-multiple per sample.
        threshold: probability threshold for approval.
        min_trades: return sentinel if fewer than this many trades approved.
        annualization: Sharpe scaling factor (52 for weekly, 252 for daily).

    Returns:
        Sharpe ratio, or -99.0 if fewer than min_trades approved.
    """
    if len(y_true) != len(y_pred_proba) or len(y_true) != len(realized_r):
        raise ValueError(
            f"Length mismatch: y_true={len(y_true)}, "
            f"y_pred={len(y_pred_proba)}, r={len(realized_r)}"
        )
    if len(y_true) == 0:
        return -99.0

    approved = np.asarray(y_pred_proba) >= threshold
    n_approved = int(approved.sum())
    if n_approved < min_trades:
        return -99.0

    r_series = np.asarray(realized_r)[approved]
    mean_r = float(r_series.mean())
    std_r = float(r_series.std(ddof=0))
    if std_r == 0 or not np.isfinite(std_r):
        return 0.0

    return (mean_r / std_r) * np.sqrt(annualization)


def precision_at_threshold(
    y_true: np.ndarray,
    y_pred_proba: np.ndarray,
    threshold: float = 0.55,
) -> float:
    """Fraction of approved trades that were wins. NaN if none approved."""
    approved = np.asarray(y_pred_proba) >= threshold
    if approved.sum() == 0:
        return float("nan")
    return float(np.asarray(y_true)[approved].mean())


def recall_at_threshold(
    y_true: np.ndarray,
    y_pred_proba: np.ndarray,
    threshold: float = 0.55,
) -> float:
    """Fraction of actual wins that the model approved."""
    y_true = np.asarray(y_true)
    if y_true.sum() == 0:
        return float("nan")
    approved = np.asarray(y_pred_proba) >= threshold
    true_positives = int((approved & (y_true == 1)).sum())
    return true_positives / int(y_true.sum())
