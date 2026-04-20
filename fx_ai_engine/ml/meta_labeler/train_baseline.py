"""Baseline meta-labeler: logistic regression with walk-forward CV.

Purpose: establish a credibility floor BEFORE training LightGBM.

If logistic regression with class_weight='balanced' can't achieve a
median walk-forward Sharpe >= 0.3 and AUC >= 0.53, then:
    - the features contain no learnable signal, OR
    - the labels are corrupted (look-ahead, mislabeled wins/losses), OR
    - the rule set produces fundamentally random outcomes.

In any of those cases, LightGBM will just overfit noise. We stop and
iterate on features/labels/rules — not on models.

MLflow is optional: if installed, we log runs; otherwise we skip.
"""
from __future__ import annotations

import logging
import warnings
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from ml.meta_labeler.validation import (
    WalkForwardFold,
    cv_sharpe,
    generate_walk_forward_folds,
    precision_at_threshold,
    recall_at_threshold,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Acceptance thresholds (Definition of Done for Phase 1.6)
# ══════════════════════════════════════════════════════════════════════

# If ANY of these fails, the baseline is considered insufficient and
# the user should NOT proceed to LightGBM training.
MIN_MEDIAN_SHARPE: float = 0.3
MIN_MEDIAN_AUC: float = 0.53
MAX_SHARPE_IQR: float = 1.5


@dataclass
class FoldResult:
    """Metrics for a single walk-forward fold."""
    fold_index: int
    train_size: int
    test_size: int
    sharpe: float
    auc: float
    precision: float
    recall: float
    n_approved: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BaselineResult:
    """Aggregate result of a walk-forward baseline run."""
    fold_results: list[FoldResult] = field(default_factory=list)
    median_sharpe: float = float("nan")
    median_auc: float = float("nan")
    sharpe_iqr: float = float("nan")
    passes_thresholds: bool = False
    failure_reasons: list[str] = field(default_factory=list)
    n_folds: int = 0
    n_total_samples: int = 0

    def summary(self) -> str:
        lines = [
            f"Walk-forward baseline: {self.n_folds} folds, {self.n_total_samples} samples",
            f"  Median Sharpe: {self.median_sharpe:.3f}  (min acceptable: {MIN_MEDIAN_SHARPE})",
            f"  Median AUC:    {self.median_auc:.3f}  (min acceptable: {MIN_MEDIAN_AUC})",
            f"  Sharpe IQR:    {self.sharpe_iqr:.3f}  (max acceptable: {MAX_SHARPE_IQR})",
            f"  Thresholds:    {'PASS' if self.passes_thresholds else 'FAIL'}",
        ]
        if self.failure_reasons:
            lines.append("  Failures:")
            for r in self.failure_reasons:
                lines.append(f"    - {r}")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# MLflow helper (optional)
# ══════════════════════════════════════════════════════════════════════

def _get_mlflow():
    """Return mlflow module if installed, else None."""
    try:
        import mlflow  # type: ignore[import]
        return mlflow
    except ImportError:
        return None


# ══════════════════════════════════════════════════════════════════════
# Training
# ══════════════════════════════════════════════════════════════════════

def train_baseline(
    features: pd.DataFrame,
    labels: pd.Series,
    realized_r: pd.Series,
    *,
    train_months: int = 24,
    test_months: int = 3,
    step_months: int = 1,
    purge_days: int = 1,
    embargo_days: int = 1,
    threshold: float = 0.55,
    C: float = 0.5,
    mlflow_run_name: str = "baseline_logistic",
) -> BaselineResult:
    """Train a walk-forward logistic regression meta-labeler.

    Args:
        features: shape (N, K) DataFrame with DatetimeIndex.
            Each row is a candidate; each column a feature.
        labels: binary target (1=win, 0=otherwise), aligned to `features`.
        realized_r: realized R-multiple per candidate, aligned to `features`.
        train_months, test_months, step_months: walk-forward config.
        purge_days, embargo_days: gap sizes around test fold.
        threshold: probability threshold for approving a trade.
        C: L2 regularization inverse strength (lower = more regularization).
        mlflow_run_name: name of the MLflow run if mlflow is installed.

    Returns:
        BaselineResult summarizing per-fold and aggregate metrics.
    """
    # Lazy sklearn import (we don't want it at module import time)
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.metrics import roc_auc_score
    except ImportError as exc:
        raise RuntimeError(
            "scikit-learn is required for baseline training. "
            "Install with: pip install scikit-learn"
        ) from exc

    if not isinstance(features.index, pd.DatetimeIndex):
        raise TypeError("features must have a DatetimeIndex")
    if len(features) != len(labels) or len(features) != len(realized_r):
        raise ValueError(
            f"Length mismatch: features={len(features)}, "
            f"labels={len(labels)}, realized_r={len(realized_r)}"
        )
    if set(labels.unique()) - {0, 1}:
        raise ValueError(f"labels must be binary (0/1), got {set(labels.unique())}")

    # Sort by timestamp (defensive)
    order = features.index.argsort()
    features = features.iloc[order]
    labels = labels.iloc[order]
    realized_r = realized_r.iloc[order]

    folds = generate_walk_forward_folds(
        features.index,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
        purge=pd.Timedelta(days=purge_days),
        embargo=pd.Timedelta(days=embargo_days),
    )

    fold_results: list[FoldResult] = []

    mlflow = _get_mlflow()
    mlflow_ctx = mlflow.start_run(run_name=mlflow_run_name) if mlflow else None
    try:
        if mlflow:
            mlflow.log_params({
                "model": "LogisticRegression",
                "C": C,
                "class_weight": "balanced",
                "train_months": train_months,
                "test_months": test_months,
                "step_months": step_months,
                "purge_days": purge_days,
                "embargo_days": embargo_days,
                "threshold": threshold,
                "n_folds": len(folds),
                "n_features": features.shape[1],
                "n_samples": len(features),
            })

        for fold in folds:
            result = _train_one_fold(
                fold, features, labels, realized_r,
                Pipeline, StandardScaler, LogisticRegression, roc_auc_score,
                C=C, threshold=threshold,
            )
            if result is None:
                continue
            fold_results.append(result)
            if mlflow:
                mlflow.log_metrics(
                    {
                        f"fold_{fold.fold_index}_sharpe": result.sharpe,
                        f"fold_{fold.fold_index}_auc": result.auc,
                        f"fold_{fold.fold_index}_precision": result.precision,
                    }
                )

        aggregate = _aggregate_results(fold_results, n_total=len(features))
        if mlflow:
            mlflow.log_metrics({
                "median_sharpe_wf": aggregate.median_sharpe,
                "median_auc_wf": aggregate.median_auc,
                "sharpe_iqr": aggregate.sharpe_iqr,
                "passes_thresholds": int(aggregate.passes_thresholds),
            })
        return aggregate
    finally:
        if mlflow_ctx is not None:
            mlflow_ctx.__exit__(None, None, None)


def _train_one_fold(
    fold: WalkForwardFold,
    features: pd.DataFrame,
    labels: pd.Series,
    realized_r: pd.Series,
    Pipeline,
    StandardScaler,
    LogisticRegression,
    roc_auc_score,
    *,
    C: float,
    threshold: float,
) -> Optional[FoldResult]:
    X_tr = features.iloc[fold.train_indices]
    y_tr = labels.iloc[fold.train_indices]
    X_te = features.iloc[fold.test_indices]
    y_te = labels.iloc[fold.test_indices]
    r_te = realized_r.iloc[fold.test_indices]

    # Need at least one of each class in training
    if len(set(y_tr.unique()) & {0, 1}) < 2:
        logger.warning(
            "Fold %d has only one class in training — skipping",
            fold.fold_index,
        )
        return None
    if len(y_te) == 0:
        return None

    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(
            class_weight="balanced",
            max_iter=1000,
            C=C,
            solver="lbfgs",
        )),
    ])

    with warnings.catch_warnings():
        # sklearn occasionally warns about convergence on small folds;
        # we already set max_iter=1000 and surface failures explicitly.
        warnings.simplefilter("ignore")
        pipe.fit(X_tr, y_tr)

    proba = pipe.predict_proba(X_te)[:, 1]

    # AUC undefined if only one class in test
    try:
        auc = float(roc_auc_score(y_te, proba)) if len(set(y_te.unique())) > 1 else float("nan")
    except ValueError:
        auc = float("nan")

    sharpe = cv_sharpe(y_te.values, proba, r_te.values, threshold=threshold)
    precision = precision_at_threshold(y_te.values, proba, threshold=threshold)
    recall = recall_at_threshold(y_te.values, proba, threshold=threshold)
    n_approved = int((proba >= threshold).sum())

    return FoldResult(
        fold_index=fold.fold_index,
        train_size=fold.n_train,
        test_size=fold.n_test,
        sharpe=float(sharpe),
        auc=auc,
        precision=float(precision) if np.isfinite(precision) else float("nan"),
        recall=float(recall) if np.isfinite(recall) else float("nan"),
        n_approved=n_approved,
    )


def _aggregate_results(
    fold_results: list[FoldResult],
    n_total: int,
) -> BaselineResult:
    if not fold_results:
        return BaselineResult(
            n_total_samples=n_total,
            failure_reasons=["No folds produced any results"],
        )

    # Filter out sentinel Sharpes (-99.0 means "too few trades approved")
    usable_sharpes = [
        fr.sharpe for fr in fold_results
        if np.isfinite(fr.sharpe) and fr.sharpe > -50.0
    ]
    usable_aucs = [fr.auc for fr in fold_results if np.isfinite(fr.auc)]

    median_sharpe = float(np.median(usable_sharpes)) if usable_sharpes else float("nan")
    median_auc = float(np.median(usable_aucs)) if usable_aucs else float("nan")
    sharpe_iqr = (
        float(np.percentile(usable_sharpes, 75) - np.percentile(usable_sharpes, 25))
        if len(usable_sharpes) >= 4 else float("nan")
    )

    failures: list[str] = []
    if not np.isfinite(median_sharpe) or median_sharpe < MIN_MEDIAN_SHARPE:
        failures.append(
            f"median Sharpe {median_sharpe:.3f} < {MIN_MEDIAN_SHARPE}"
        )
    if not np.isfinite(median_auc) or median_auc < MIN_MEDIAN_AUC:
        failures.append(
            f"median AUC {median_auc:.3f} < {MIN_MEDIAN_AUC}"
        )
    if np.isfinite(sharpe_iqr) and sharpe_iqr > MAX_SHARPE_IQR:
        failures.append(
            f"Sharpe IQR {sharpe_iqr:.3f} > {MAX_SHARPE_IQR} — folds disagree too much"
        )

    return BaselineResult(
        fold_results=fold_results,
        median_sharpe=median_sharpe,
        median_auc=median_auc,
        sharpe_iqr=sharpe_iqr,
        passes_thresholds=not failures,
        failure_reasons=failures,
        n_folds=len(fold_results),
        n_total_samples=n_total,
    )
