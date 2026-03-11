"""Bridge health diagnostic — run standalone or import check_snapshot_health()."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_SNAPSHOT_STALE_SECONDS = 180


def check_snapshot_health(bridge_path: Path | str | None = None) -> dict:
    """Return health dict: status (FRESH/STALE/MISSING/CORRUPT), age_seconds, balance, path."""
    if bridge_path is None:
        from core.bridge_utils import get_mt5_bridge_path
        bridge_path = get_mt5_bridge_path()
    bridge_path = Path(bridge_path)
    snap = bridge_path / "feedback" / "account_snapshot.json"

    if not snap.exists():
        return {"status": "MISSING", "age_seconds": None, "balance": None, "path": str(snap)}

    age = time.time() - snap.stat().st_mtime
    try:
        data = json.loads(snap.read_text(encoding="utf-8"))
        balance = float(data.get("balance", 0.0))
    except (json.JSONDecodeError, OSError):
        return {"status": "CORRUPT", "age_seconds": round(age, 1), "balance": None, "path": str(snap)}

    status = "FRESH" if age < _SNAPSHOT_STALE_SECONDS else "STALE"
    return {"status": status, "age_seconds": round(age, 1), "balance": balance, "path": str(snap)}


def check_ea_deployment_drift(
    bridge_path: Path | str | None = None,
    *,
    repo_ea_path: Path | str | None = None,
) -> dict:
    if bridge_path is None:
        from core.bridge_utils import get_mt5_bridge_path

        bridge_path = get_mt5_bridge_path()
    bridge_path = Path(bridge_path)
    repo_path = Path(repo_ea_path) if repo_ea_path is not None else Path(__file__).resolve().parent / "mt5_ea" / "FX_Execution.mq5"
    deployed_path = bridge_path.parent.parent / "Experts" / "FX_Execution.mq5"

    result = {
        "repo_path": str(repo_path),
        "deployed_path": str(deployed_path),
    }
    if not repo_path.exists():
        return {**result, "status": "MISSING_REPO"}
    if not deployed_path.exists():
        return {**result, "status": "MISSING_DEPLOYED"}

    status = "IN_SYNC" if repo_path.read_bytes() == deployed_path.read_bytes() else "DRIFT"
    return {
        **result,
        "status": status,
        "repo_mtime": round(repo_path.stat().st_mtime, 3),
        "deployed_mtime": round(deployed_path.stat().st_mtime, 3),
    }


def main() -> None:
    os.environ.setdefault("USE_MT5_MOCK", "1")
    from core.bridge_utils import get_mt5_bridge_path

    bridge_path = get_mt5_bridge_path()
    print(f"\n{'='*60}")
    print("BRIDGE HEALTH CHECK")
    print(f"{'='*60}")
    print(f"Bridge path: {bridge_path}")
    print(f"  pending_signals/  exists: {(bridge_path / 'pending_signals').exists()}")
    print(f"  feedback/         exists: {(bridge_path / 'feedback').exists()}")
    print(f"  exits/            exists: {(bridge_path / 'exits').exists()}")
    print(f"  active_locks/     exists: {(bridge_path / 'active_locks').exists()}")

    result = check_snapshot_health(bridge_path)
    print(f"\nAccount snapshot: {result['path']}")
    print(f"  Status:      {result['status']}")
    if result["age_seconds"] is not None:
        print(f"  Age:         {result['age_seconds']:.0f}s")
    if result["balance"] is not None:
        print(f"  Balance:     ${result['balance']:.2f}")

    drift = check_ea_deployment_drift(bridge_path)
    print("\nEA deployment:")
    print(f"  Repo source:     {drift['repo_path']}")
    print(f"  MT5 deployed EA: {drift['deployed_path']}")
    print(f"  Status:          {drift['status']}")

    pending_dir = bridge_path / "pending_signals"
    if pending_dir.exists():
        stuck = list(pending_dir.glob("*.json"))
        print(f"\nStuck pending signals: {len(stuck)}")
        for f in stuck[:5]:
            print(f"  {f.name}")
        if len(stuck) > 5:
            print(f"  ... and {len(stuck)-5} more")

    print(f"\n{'='*60}")
    if result["status"] == "FRESH":
        print("VERDICT: Bridge is HEALTHY")
        print("  EA is writing snapshots correctly.")
        print("  If STATE_STALE still occurs, verify BRIDGE_BASE_PATH env var points here.")
    elif result["status"] == "STALE":
        print("VERDICT: Bridge is STALE")
        print(f"  Snapshot exists but is >{_SNAPSHOT_STALE_SECONDS}s old.")
        print("  FIX: Verify MT5 EA is attached to a chart with 'Allow Algo Trading' ON.")
        print(f"  FIX: Set BRIDGE_BASE_PATH={bridge_path}")
    elif result["status"] == "MISSING":
        print("VERDICT: Bridge is DISCONNECTED")
        print("  No account_snapshot.json found.")
        print("  FIX: Check BRIDGE_BASE_PATH points to correct MT5 MQL5/Files/bridge folder.")
        print("  FIX: Compile and attach FX_Execution.mq5 to any chart in MT5.")
    elif result["status"] == "CORRUPT":
        print("VERDICT: Bridge snapshot is CORRUPT")
        print("  File exists but JSON is invalid.")
        print("  FIX: Delete the corrupt file and restart the EA.")
    if drift["status"] == "DRIFT":
        print("  ACTION: Copy/compile the repo FX_Execution.mq5 into the MT5 Experts folder before relying on live exit feedback or auto stop-management.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
