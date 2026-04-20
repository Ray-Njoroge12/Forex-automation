"""Offline training orchestration for the meta-labeler baseline.

This stage consumes labeled replay artifacts, builds feature vectors with the
same FeatureBuilder contract used in live inference, runs walk-forward
baseline training, and persists deterministic artifacts.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ml.features import FEATURE_ORDER, FEATURE_SCHEMA_VERSION, FeatureBuilder
from ml.meta_labeler.adapters import normalize_ohlcv_columns, normalize_regime_label_for_features
from ml.meta_labeler.artifact_paths import (
    DEFAULT_TRAINING_ARTIFACT_ROOT,
    TrainingArtifactPaths,
    build_training_artifact_paths,
)
from ml.meta_labeler.extract_data import DEFAULT_DATA_DIR, load_parquet
from ml.meta_labeler.train_baseline import BaselineResult, train_baseline

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfflineTrainingConfig:
    """Inputs for running one offline baseline-training pass."""

    symbol: str
    data_dir: Path = DEFAULT_DATA_DIR
    output_root: Path = DEFAULT_TRAINING_ARTIFACT_ROOT
    run_id: str | None = None
    replay_run_dir: Path | None = None
    labeled_path: Path | None = None
    m15_df: pd.DataFrame | None = None
    h1_df: pd.DataFrame | None = None
    labeled_df: pd.DataFrame | None = None
    train_months: int = 24
    test_months: int = 3
    step_months: int = 1
    purge_days: int = 1
    embargo_days: int = 1
    threshold: float = 0.55
    C: float = 0.5
    mlflow_run_name: str = "baseline_logistic"


@dataclass(frozen=True)
class OfflineTrainingResult:
    """Summary output from one offline training run."""

    symbol: str
    run_id: str
    paths: TrainingArtifactPaths
    n_feature_rows: int
    n_skipped_feature_rows: int
    baseline: BaselineResult


def run_offline_training(config: OfflineTrainingConfig) -> OfflineTrainingResult:
    """Run labeled-data feature build + baseline training and persist artifacts."""
    symbol = _normalize_symbol(config.symbol)
    labeled_df = _resolve_labeled_dataframe(config)
    m15_df, h1_df = _resolve_market_dataframes(config, symbol)
    features, labels, realized_r, skipped = _build_training_matrix(
        symbol,
        m15_df,
        h1_df,
        labeled_df,
    )

    if len(features) == 0:
        raise ValueError("No feature rows were produced from labeled inputs")

    baseline = train_baseline(
        features,
        labels,
        realized_r,
        train_months=config.train_months,
        test_months=config.test_months,
        step_months=config.step_months,
        purge_days=config.purge_days,
        embargo_days=config.embargo_days,
        threshold=config.threshold,
        C=config.C,
        mlflow_run_name=config.mlflow_run_name,
    )

    paths = build_training_artifact_paths(config.output_root, run_id=config.run_id)
    features.to_parquet(paths.features_path, compression="zstd", index=True)
    labels.to_frame(name="binary_label").to_parquet(
        paths.labels_path,
        compression="zstd",
        index=True,
    )
    realized_r.to_frame(name="realized_r").to_parquet(
        paths.realized_r_path,
        compression="zstd",
        index=True,
    )
    _write_json(paths.fold_metrics_path, [fold.to_dict() for fold in baseline.fold_results])

    summary_payload = {
        "symbol": symbol,
        "run_id": paths.run_id,
        "median_sharpe": baseline.median_sharpe,
        "median_auc": baseline.median_auc,
        "sharpe_iqr": baseline.sharpe_iqr,
        "passes_thresholds": baseline.passes_thresholds,
        "failure_reasons": list(baseline.failure_reasons),
        "n_folds": baseline.n_folds,
        "n_total_samples": baseline.n_total_samples,
        "n_feature_rows": len(features),
        "n_skipped_feature_rows": skipped,
    }
    _write_json(paths.summary_path, summary_payload)

    checkpoint_payload = _persist_baseline_checkpoint(
        paths.checkpoint_path,
        symbol,
        features,
        labels,
        config.C,
    )

    _write_json(
        paths.run_manifest_path,
        {
            "symbol": symbol,
            "run_id": paths.run_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "inputs": {
                "labeled_source": _labeled_source(config),
                "market_source": _market_source(config),
                "data_dir": str(config.data_dir),
            },
            "config": {
                "train_months": config.train_months,
                "test_months": config.test_months,
                "step_months": config.step_months,
                "purge_days": config.purge_days,
                "embargo_days": config.embargo_days,
                "threshold": config.threshold,
                "C": config.C,
                "mlflow_run_name": config.mlflow_run_name,
            },
            "counts": {
                "n_feature_rows": len(features),
                "n_skipped_feature_rows": skipped,
                "n_folds": baseline.n_folds,
            },
            "baseline_summary": summary_payload,
            "checkpoint": checkpoint_payload,
            "artifacts": {
                "run_dir": str(paths.run_dir),
                "features": str(paths.features_path),
                "labels": str(paths.labels_path),
                "realized_r": str(paths.realized_r_path),
                "fold_metrics": str(paths.fold_metrics_path),
                "summary": str(paths.summary_path),
                "checkpoint": str(paths.checkpoint_path),
                "run_manifest": str(paths.run_manifest_path),
            },
        },
    )

    logger.info(
        "Offline training complete for %s. rows=%d folds=%d run_id=%s",
        symbol,
        len(features),
        baseline.n_folds,
        paths.run_id,
    )
    return OfflineTrainingResult(
        symbol=symbol,
        run_id=paths.run_id,
        paths=paths,
        n_feature_rows=len(features),
        n_skipped_feature_rows=skipped,
        baseline=baseline,
    )


def _resolve_labeled_dataframe(config: OfflineTrainingConfig) -> pd.DataFrame:
    if config.labeled_df is not None:
        labeled_df = config.labeled_df.copy()
    else:
        labeled_path = _resolve_labeled_path(config)
        labeled_df = pd.read_parquet(labeled_path)

    if not isinstance(labeled_df.index, pd.DatetimeIndex):
        raise TypeError("labeled_df must have a DatetimeIndex")
    required_cols = {"binary_label", "realized_r"}
    missing_cols = required_cols - set(labeled_df.columns)
    if missing_cols:
        raise ValueError(f"labeled_df missing required columns: {sorted(missing_cols)}")
    return labeled_df.sort_index(kind="mergesort")


def _resolve_labeled_path(config: OfflineTrainingConfig) -> Path:
    if config.labeled_path is not None:
        return Path(config.labeled_path)
    if config.replay_run_dir is not None:
        return Path(config.replay_run_dir) / "labeled.parquet"
    raise ValueError("Provide one of labeled_df, labeled_path, or replay_run_dir")


def _resolve_market_dataframes(
    config: OfflineTrainingConfig,
    symbol: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    m15_df = config.m15_df.copy() if config.m15_df is not None else load_parquet(symbol, "M15", config.data_dir)
    h1_df = config.h1_df.copy() if config.h1_df is not None else load_parquet(symbol, "H1", config.data_dir)

    m15_df = normalize_ohlcv_columns(m15_df).sort_index(kind="mergesort")
    h1_df = normalize_ohlcv_columns(h1_df).sort_index(kind="mergesort")
    _validate_market_dataframe(m15_df, name="m15_df")
    _validate_market_dataframe(h1_df, name="h1_df")
    return m15_df, h1_df


def _build_training_matrix(
    symbol: str,
    m15_df: pd.DataFrame,
    h1_df: pd.DataFrame,
    labeled_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series, pd.Series, int]:
    builder = FeatureBuilder(m15_df, h1_df, symbol)

    feature_rows: list[np.ndarray] = []
    labels: list[int] = []
    realized_values: list[float] = []
    timestamps: list[pd.Timestamp] = []
    skipped = 0

    for ts, row in labeled_df.iterrows():
        regime_label = normalize_regime_label_for_features(str(row.get("regime", "UNKNOWN")))
        try:
            vector = builder.build(ts, regime_label=regime_label)
        except (RuntimeError, ValueError):
            skipped += 1
            continue

        feature_rows.append(vector.values)
        labels.append(int(row["binary_label"]))
        realized_values.append(float(row["realized_r"]))
        timestamps.append(ts)

    if not feature_rows:
        return pd.DataFrame(columns=list(FEATURE_ORDER)), pd.Series(dtype="int64"), pd.Series(dtype="float64"), skipped

    features = pd.DataFrame(
        feature_rows,
        columns=list(FEATURE_ORDER),
        index=pd.DatetimeIndex(timestamps),
    ).sort_index(kind="mergesort")
    labels_series = pd.Series(labels, index=features.index, dtype="int64")
    realized_series = pd.Series(realized_values, index=features.index, dtype="float64")
    return features, labels_series, realized_series, skipped


def _persist_baseline_checkpoint(
    checkpoint_path: Path,
    symbol: str,
    features: pd.DataFrame,
    labels: pd.Series,
    C: float,
) -> dict[str, object]:
    try:
        import joblib
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise RuntimeError(
            "Checkpoint persistence requires joblib and scikit-learn"
        ) from exc

    class_counts = {str(k): int(v) for k, v in labels.value_counts().to_dict().items()}
    checkpoint: dict[str, object] = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "feature_order": list(FEATURE_ORDER),
        "symbol": symbol,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_rows": len(features),
        "class_counts": class_counts,
        "model_type": "LogisticRegression",
    }

    if labels.nunique() >= 2:
        model = Pipeline(
            [
                ("scaler", StandardScaler()),
                (
                    "lr",
                    LogisticRegression(
                        class_weight="balanced",
                        max_iter=1000,
                        C=C,
                        solver="lbfgs",
                        random_state=42,
                    ),
                ),
            ]
        )
        model.fit(features, labels)
        checkpoint["model"] = model
        checkpoint["model_status"] = "fitted"
    else:
        checkpoint["model"] = None
        checkpoint["model_status"] = "skipped_single_class"

    joblib.dump(checkpoint, checkpoint_path)
    return {
        "model_status": checkpoint["model_status"],
        "class_counts": class_counts,
        "n_rows": len(features),
    }


def _validate_market_dataframe(df: pd.DataFrame, *, name: str) -> None:
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError(f"{name} must have a DatetimeIndex")
    if df.empty:
        raise ValueError(f"{name} must not be empty")
    required_cols = {"open", "high", "low", "close"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        raise ValueError(f"{name} missing required columns: {sorted(missing_cols)}")


def _normalize_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        raise ValueError("symbol is required")
    return normalized


def _labeled_source(config: OfflineTrainingConfig) -> str:
    if config.labeled_df is not None:
        return "in_memory"
    if config.labeled_path is not None:
        return "path"
    if config.replay_run_dir is not None:
        return "replay_run_dir"
    return "unknown"


def _market_source(config: OfflineTrainingConfig) -> str:
    if config.m15_df is not None or config.h1_df is not None:
        return "in_memory"
    return "parquet"


def _write_json(path: Path, payload: dict | list) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    """CLI entrypoint for offline baseline training orchestration."""
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s - %(message)s",
    )
    parser = argparse.ArgumentParser(description="Run offline baseline training pipeline.")
    parser.add_argument("--symbol", required=True, help="Trading symbol, for example EURUSD")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Parquet input directory")
    parser.add_argument("--replay-run-dir", type=Path, default=None, help="Replay run directory that contains labeled.parquet")
    parser.add_argument("--labeled-path", type=Path, default=None, help="Explicit labeled parquet path")
    parser.add_argument("--out", type=Path, default=DEFAULT_TRAINING_ARTIFACT_ROOT, help="Training artifact root directory")
    parser.add_argument("--run-id", default=None, help="Optional explicit run id")
    parser.add_argument("--train-months", type=int, default=24)
    parser.add_argument("--test-months", type=int, default=3)
    parser.add_argument("--step-months", type=int, default=1)
    parser.add_argument("--purge-days", type=int, default=1)
    parser.add_argument("--embargo-days", type=int, default=1)
    parser.add_argument("--threshold", type=float, default=0.55)
    parser.add_argument("--c", type=float, default=0.5)
    args = parser.parse_args()

    result = run_offline_training(
        OfflineTrainingConfig(
            symbol=args.symbol,
            data_dir=args.data_dir,
            output_root=args.out,
            run_id=args.run_id,
            replay_run_dir=args.replay_run_dir,
            labeled_path=args.labeled_path,
            train_months=args.train_months,
            test_months=args.test_months,
            step_months=args.step_months,
            purge_days=args.purge_days,
            embargo_days=args.embargo_days,
            threshold=args.threshold,
            C=args.c,
        )
    )
    print(f"run_id={result.run_id}")
    print(f"artifacts={result.paths.run_dir}")
    print(f"features={result.n_feature_rows}")
    print(f"folds={result.baseline.n_folds}")
    print(f"passes_thresholds={result.baseline.passes_thresholds}")


if __name__ == "__main__":
    main()
