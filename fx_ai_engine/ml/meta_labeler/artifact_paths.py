"""Deterministic artifact path helpers for offline replay/training runs.

The helpers in this module centralize where replay outputs are written so
every run has a predictable, auditable footprint.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REPLAY_ARTIFACT_ROOT = Path("ml/artifacts/replay")
DEFAULT_TRAINING_ARTIFACT_ROOT = Path("ml/artifacts/training")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ReplayArtifactPaths:
    """Resolved artifact locations for one offline replay run."""

    run_id: str
    run_dir: Path
    candidates_path: Path
    labeled_path: Path
    summary_path: Path
    run_manifest_path: Path
    source_manifest_path: Path


@dataclass(frozen=True)
class TrainingArtifactPaths:
    """Resolved artifact locations for one offline training run."""

    run_id: str
    run_dir: Path
    features_path: Path
    labels_path: Path
    realized_r_path: Path
    fold_metrics_path: Path
    summary_path: Path
    checkpoint_path: Path
    run_manifest_path: Path


def generate_replay_run_id(now: datetime | None = None) -> str:
    """Return a UTC timestamp run id in YYYYMMDDTHHMMSSZ format."""
    ts = now or datetime.now(timezone.utc)
    ts_utc = ts.astimezone(timezone.utc)
    return ts_utc.strftime("%Y%m%dT%H%M%SZ")


def build_replay_artifact_paths(
    output_root: Path = DEFAULT_REPLAY_ARTIFACT_ROOT,
    *,
    run_id: str | None = None,
    create_dirs: bool = True,
    allow_existing: bool = False,
) -> ReplayArtifactPaths:
    """Build standardized paths for one replay run.

    Args:
        output_root: root directory that contains replay run folders.
        run_id: optional explicit run id. When omitted, uses UTC timestamp.
        create_dirs: create the run directory immediately.
        allow_existing: allow reusing an existing run directory.
    """
    resolved_run_id = _validate_run_id(run_id or generate_replay_run_id())
    run_dir = Path(output_root) / resolved_run_id

    if create_dirs:
        run_dir.mkdir(parents=True, exist_ok=allow_existing)

    return ReplayArtifactPaths(
        run_id=resolved_run_id,
        run_dir=run_dir,
        candidates_path=run_dir / "candidates.parquet",
        labeled_path=run_dir / "labeled.parquet",
        summary_path=run_dir / "replay_summary.json",
        run_manifest_path=run_dir / "run_manifest.json",
        source_manifest_path=run_dir / "source_manifest.json",
    )


def build_training_artifact_paths(
    output_root: Path = DEFAULT_TRAINING_ARTIFACT_ROOT,
    *,
    run_id: str | None = None,
    create_dirs: bool = True,
    allow_existing: bool = False,
) -> TrainingArtifactPaths:
    """Build standardized paths for one training run."""
    resolved_run_id = _validate_run_id(run_id or generate_replay_run_id())
    run_dir = Path(output_root) / resolved_run_id

    if create_dirs:
        run_dir.mkdir(parents=True, exist_ok=allow_existing)

    return TrainingArtifactPaths(
        run_id=resolved_run_id,
        run_dir=run_dir,
        features_path=run_dir / "features.parquet",
        labels_path=run_dir / "labels.parquet",
        realized_r_path=run_dir / "realized_r.parquet",
        fold_metrics_path=run_dir / "fold_metrics.json",
        summary_path=run_dir / "baseline_summary.json",
        checkpoint_path=run_dir / "baseline_model.joblib",
        run_manifest_path=run_dir / "run_manifest.json",
    )


def _validate_run_id(run_id: str) -> str:
    candidate = str(run_id or "").strip()
    if not candidate:
        raise ValueError("run_id must be a non-empty string")
    if not _RUN_ID_RE.fullmatch(candidate):
        raise ValueError(
            "run_id may only contain letters, numbers, dash, and underscore"
        )
    return candidate
