"""Tests for artifact path helpers used by offline replay orchestration."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ml.meta_labeler.artifact_paths import (
    build_replay_artifact_paths,
    build_training_artifact_paths,
    generate_replay_run_id,
)


def test_generate_replay_run_id_is_utc_timestamp() -> None:
    run_id = generate_replay_run_id(datetime(2026, 4, 20, 7, 8, 9, tzinfo=timezone.utc))
    assert run_id == "20260420T070809Z"


def test_build_replay_artifact_paths_creates_expected_paths(tmp_path) -> None:
    paths = build_replay_artifact_paths(tmp_path, run_id="run_001")
    assert paths.run_id == "run_001"
    assert paths.run_dir == tmp_path / "run_001"
    assert paths.run_dir.exists()
    assert paths.candidates_path == paths.run_dir / "candidates.parquet"
    assert paths.labeled_path == paths.run_dir / "labeled.parquet"
    assert paths.summary_path == paths.run_dir / "replay_summary.json"
    assert paths.run_manifest_path == paths.run_dir / "run_manifest.json"
    assert paths.source_manifest_path == paths.run_dir / "source_manifest.json"


def test_build_replay_artifact_paths_rejects_invalid_run_id(tmp_path) -> None:
    with pytest.raises(ValueError, match="run_id"):
        build_replay_artifact_paths(tmp_path, run_id="bad run id")


def test_build_replay_artifact_paths_refuses_existing_run_dir(tmp_path) -> None:
    build_replay_artifact_paths(tmp_path, run_id="repeatable")
    with pytest.raises(FileExistsError):
        build_replay_artifact_paths(tmp_path, run_id="repeatable")


def test_build_training_artifact_paths_creates_expected_paths(tmp_path) -> None:
    paths = build_training_artifact_paths(tmp_path, run_id="train_001")
    assert paths.run_id == "train_001"
    assert paths.run_dir == tmp_path / "train_001"
    assert paths.run_dir.exists()
    assert paths.features_path == paths.run_dir / "features.parquet"
    assert paths.labels_path == paths.run_dir / "labels.parquet"
    assert paths.realized_r_path == paths.run_dir / "realized_r.parquet"
    assert paths.fold_metrics_path == paths.run_dir / "fold_metrics.json"
    assert paths.summary_path == paths.run_dir / "baseline_summary.json"
    assert paths.checkpoint_path == paths.run_dir / "baseline_model.joblib"
    assert paths.run_manifest_path == paths.run_dir / "run_manifest.json"
