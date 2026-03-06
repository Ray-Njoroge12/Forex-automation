import json
import os
import time
from pathlib import Path
import pytest

os.environ["USE_MT5_MOCK"] = "1"


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
