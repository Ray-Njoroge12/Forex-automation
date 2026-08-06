from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bridge.signal_router import SignalRouter


def _payload(trade_id: str) -> dict:
    return {
        "trade_id": trade_id,
        "symbol": "EURUSD",
        "direction": "BUY",
        "risk_percent": 0.032,
        "stop_pips": 10.0,
        "take_profit_pips": 22.0,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def test_router_loads_legacy_json_and_log_registry(tmp_path: Path) -> None:
    registry_path = tmp_path / "bridge" / "trade_id_registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(["legacy-1"]), encoding="utf-8")
    (registry_path.parent / "trade_id_registry.log").write_text("log-1\n", encoding="utf-8")

    router = SignalRouter(
        pending_dir=tmp_path / "bridge" / "pending_signals",
        lock_dir=tmp_path / "bridge" / "active_locks",
        registry_path=registry_path,
    )

    with pytest.raises(RuntimeError, match="Duplicate trade_id blocked"):
        router.send(_payload("legacy-1"))
    with pytest.raises(RuntimeError, match="Duplicate trade_id blocked"):
        router.send(_payload("log-1"))


def test_router_appends_registry_and_blocks_duplicate_after_restart(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    registry_path = bridge_dir / "trade_id_registry.json"
    pending_dir = bridge_dir / "pending_signals"
    lock_dir = bridge_dir / "active_locks"

    router = SignalRouter(
        pending_dir=pending_dir,
        lock_dir=lock_dir,
        registry_path=registry_path,
    )
    router.send(_payload("trade-123"))

    log_path = bridge_dir / "trade_id_registry.log"
    assert log_path.exists()
    assert "trade-123" in log_path.read_text(encoding="utf-8").splitlines()

    (pending_dir / "trade-123.json").unlink()
    (lock_dir / "trade-123.lock").unlink()

    router_after_restart = SignalRouter(
        pending_dir=pending_dir,
        lock_dir=lock_dir,
        registry_path=registry_path,
    )
    with pytest.raises(RuntimeError, match="Duplicate trade_id blocked"):
        router_after_restart.send(_payload("trade-123"))
