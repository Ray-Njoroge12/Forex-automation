"""Evidence gate for strategy-parameter experiments.

This prevents an attractive result from a short, changed, or smoke-test data
set from being treated as an optimisation outcome.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from backtesting.dataset_manifest import inspect_ohlc_csv
from backtesting.simulation_profile import build_simulation_profile

MIN_OPTIMIZATION_COVERAGE_DAYS = 365


@dataclass(frozen=True)
class ExperimentReadiness:
    approved: bool
    reason_codes: tuple[str, ...]
    details: str


def assess_optimization_readiness(
    *,
    csv_path: str | Path,
    manifest_path: str | Path | None,
    symbol: str,
    mode_id: str | None,
) -> ExperimentReadiness:
    reasons: list[str] = []
    profile = build_simulation_profile(mode_id)
    if not profile.realistic_constraints:
        reasons.append("OPTIMIZATION_REQUIRES_REALISTIC_PROFILE")
    if manifest_path is None:
        reasons.append("DATASET_MANIFEST_REQUIRED")
        return ExperimentReadiness(False, tuple(reasons), "candidate experiments require a hash-validated dataset manifest")

    manifest_file = Path(manifest_path)
    if not manifest_file.is_file():
        reasons.append("DATASET_MANIFEST_MISSING")
        return ExperimentReadiness(False, tuple(reasons), f"manifest not found: {manifest_file}")
    try:
        recorded = json.loads(manifest_file.read_text(encoding="utf-8"))
        actual = inspect_ohlc_csv(csv_path, symbol=symbol).to_dict()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        reasons.append("DATASET_VALIDATION_FAILED")
        return ExperimentReadiness(False, tuple(reasons), str(exc))

    for field in ("schema_version", "symbol", "sha256", "rows", "start_utc", "end_utc"):
        if recorded.get(field) != actual[field]:
            reasons.append("DATASET_MANIFEST_MISMATCH")
            break

    start = pd.Timestamp(actual["start_utc"])
    end = pd.Timestamp(actual["end_utc"])
    coverage_days = (end - start).total_seconds() / 86400.0
    if coverage_days < MIN_OPTIMIZATION_COVERAGE_DAYS:
        reasons.append("DATASET_COVERAGE_INSUFFICIENT")

    if reasons:
        return ExperimentReadiness(
            False,
            tuple(reasons),
            f"mode={profile.mode_id} coverage_days={coverage_days:.1f} rows={actual['rows']}",
        )
    return ExperimentReadiness(
        True,
        (),
        f"mode={profile.mode_id} coverage_days={coverage_days:.1f} rows={actual['rows']} manifest={manifest_file.name}",
    )
