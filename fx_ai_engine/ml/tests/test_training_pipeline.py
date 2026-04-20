"""Tests for offline training orchestration entrypoint."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from ml.meta_labeler.training_pipeline import OfflineTrainingConfig, run_offline_training


def _make_market_bars(n: int = 4000, *, start: str = "2024-01-01 00:00") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    trend = np.linspace(1.1000, 1.2500, n)
    noise = np.sin(np.linspace(0.0, 25.0, n)) * 0.001
    close = trend + noise
    open_ = np.concatenate(([close[0]], close[:-1]))
    high = np.maximum(open_, close) + 0.0007
    low = np.minimum(open_, close) - 0.0007
    volume = np.full(n, 1200, dtype="int64")
    spread = np.full(n, 10, dtype="int32")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "spread": spread,
        },
        index=idx,
    )


def _make_labeled_df(m15_df: pd.DataFrame) -> pd.DataFrame:
    index = m15_df.index[350:3600:6]
    n = len(index)
    labels = (np.arange(n) % 3 == 0).astype("int64")
    realized_r = np.where(labels == 1, 2.1, -1.0).astype("float64")
    realized_r += np.sin(np.linspace(0.0, 10.0, n)) * 0.05
    regime = np.where(np.arange(n) % 2 == 0, "TREND_UP", "RANGE")

    return pd.DataFrame(
        {
            "binary_label": labels,
            "realized_r": realized_r,
            "regime": regime,
        },
        index=index,
    )


def test_run_offline_training_writes_expected_artifacts(tmp_path) -> None:
    m15_df = _make_market_bars()
    h1_df = _make_market_bars()
    labeled_df = _make_labeled_df(m15_df)

    result = run_offline_training(
        OfflineTrainingConfig(
            symbol="EURUSD",
            output_root=tmp_path,
            run_id="train_run",
            m15_df=m15_df,
            h1_df=h1_df,
            labeled_df=labeled_df,
            train_months=1,
            test_months=1,
            step_months=1,
            threshold=0.55,
            C=0.5,
        )
    )

    assert result.run_id == "train_run"
    assert result.paths.features_path.exists()
    assert result.paths.labels_path.exists()
    assert result.paths.realized_r_path.exists()
    assert result.paths.fold_metrics_path.exists()
    assert result.paths.summary_path.exists()
    assert result.paths.checkpoint_path.exists()
    assert result.paths.run_manifest_path.exists()
    assert result.n_feature_rows > 0

    summary = json.loads(result.paths.summary_path.read_text(encoding="utf-8"))
    required = {
        "median_sharpe",
        "median_auc",
        "sharpe_iqr",
        "passes_thresholds",
        "failure_reasons",
        "n_folds",
    }
    assert required.issubset(summary.keys())

    fold_metrics = json.loads(result.paths.fold_metrics_path.read_text(encoding="utf-8"))
    assert isinstance(fold_metrics, list)


def test_run_offline_training_reads_labeled_from_replay_run_dir(tmp_path) -> None:
    m15_df = _make_market_bars()
    h1_df = _make_market_bars()
    labeled_df = _make_labeled_df(m15_df)

    replay_run_dir = tmp_path / "replay_run"
    replay_run_dir.mkdir(parents=True)
    labeled_df.to_parquet(replay_run_dir / "labeled.parquet", compression="zstd", index=True)

    result = run_offline_training(
        OfflineTrainingConfig(
            symbol="EURUSD",
            output_root=tmp_path,
            run_id="train_from_replay",
            replay_run_dir=replay_run_dir,
            m15_df=m15_df,
            h1_df=h1_df,
            train_months=1,
            test_months=1,
            step_months=1,
        )
    )

    assert result.n_feature_rows > 0
    assert result.paths.summary_path.exists()


def test_run_offline_training_requires_labeled_source(tmp_path) -> None:
    m15_df = _make_market_bars()
    h1_df = _make_market_bars()

    with pytest.raises(ValueError, match="labeled_df"):
        run_offline_training(
            OfflineTrainingConfig(
                symbol="EURUSD",
                output_root=tmp_path,
                m15_df=m15_df,
                h1_df=h1_df,
                train_months=1,
                test_months=1,
            )
        )
