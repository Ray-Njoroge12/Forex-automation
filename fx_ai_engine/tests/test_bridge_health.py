from __future__ import annotations

import json
import os
import time
from pathlib import Path
import pytest


def test_bridge_health_detects_missing_snapshot(tmp_path):
    """Health check must report MISSING when no snapshot exists."""
    from check_bridge_health import check_snapshot_health
    result = check_snapshot_health(bridge_path=tmp_path / "bridge")
    assert result["status"] == "MISSING"
    assert result["age_seconds"] is None


def test_bridge_health_reports_fresh_snapshot(tmp_path):
    """Health check must report FRESH when snapshot is <180s old."""
    from check_bridge_health import check_snapshot_health
    feedback = tmp_path / "bridge" / "feedback"
    feedback.mkdir(parents=True)
    snapshot = {
        "timestamp": "2026-01-01 12:00:00",
        "balance": 10.0,
        "equity": 10.0,
        "margin_free": 10.0,
        "open_positions_count": 0,
        "floating_pnl": 0.0,
    }
    (feedback / "account_snapshot.json").write_text(json.dumps(snapshot))
    result = check_snapshot_health(bridge_path=tmp_path / "bridge")
    assert result["status"] == "FRESH"
    assert result["balance"] == 10.0


def test_bridge_health_detects_stale_snapshot(tmp_path):
    """Health check must report STALE when snapshot mtime is >180s old."""
    from check_bridge_health import check_snapshot_health
    feedback = tmp_path / "bridge" / "feedback"
    feedback.mkdir(parents=True)
    snap_path = feedback / "account_snapshot.json"
    snap_path.write_text(json.dumps({
        "timestamp": "2026-01-01 10:00:00",
        "balance": 10.0, "equity": 10.0,
        "margin_free": 10.0, "open_positions_count": 0, "floating_pnl": 0.0,
    }))
    # Backdate mtime by 300 seconds
    old_time = time.time() - 300
    os.utime(snap_path, (old_time, old_time))
    result = check_snapshot_health(bridge_path=tmp_path / "bridge")
    assert result["status"] == "STALE"
    assert result["age_seconds"] > 180


def test_ea_deployment_drift_detects_mismatch(tmp_path):
    from check_bridge_health import check_ea_deployment_drift

    bridge_path = tmp_path / "MQL5" / "Files" / "bridge"
    bridge_path.mkdir(parents=True)
    repo_ea = tmp_path / "repo" / "FX_Execution.mq5"
    repo_ea.parent.mkdir(parents=True)
    repo_ea.write_text("repo version", encoding="utf-8")

    deployed_ea = tmp_path / "MQL5" / "Experts" / "FX_Execution.mq5"
    deployed_ea.parent.mkdir(parents=True)
    deployed_ea.write_text("older deployed version", encoding="utf-8")

    result = check_ea_deployment_drift(bridge_path=bridge_path, repo_ea_path=repo_ea)

    assert result["status"] == "DRIFT"
    assert Path(result["repo_path"]).name == "FX_Execution.mq5"
    assert Path(result["repo_path"]).parent.name == "repo"
    assert Path(result["deployed_path"]).name == "FX_Execution.mq5"
    assert Path(result["deployed_path"]).parent.name == "Experts"
