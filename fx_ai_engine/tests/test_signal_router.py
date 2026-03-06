from __future__ import annotations

import json
import os
import time

import pytest

from bridge.signal_router import SignalRouter


@pytest.fixture
def valid_payload() -> dict:
    return {
        "trade_id": "AI_20260225_120000_ab12cd",
        "symbol": "EURUSD",
        "direction": "BUY",
        "risk_percent": 0.02,
        "stop_pips": 12.0,
        "take_profit_pips": 26.4,
        "timestamp_utc": "2026-02-25T12:00:00+00:00",
    }


def test_router_writes_atomically_and_persists_registry(tmp_path, valid_payload: dict) -> None:
    pending = tmp_path / "pending"
    locks = tmp_path / "locks"
    registry = tmp_path / "trade_id_registry.json"

    router = SignalRouter(pending_dir=pending, lock_dir=locks, registry_path=registry)
    written_path = router.send(valid_payload)

    assert written_path.exists()
    assert written_path.suffix == ".json"
    assert not (pending / (written_path.stem + ".json.tmp")).exists()

    payload = json.loads(written_path.read_text(encoding="utf-8"))
    assert payload["trade_id"] == valid_payload["trade_id"]

    registry_payload = json.loads(registry.read_text(encoding="utf-8"))
    assert valid_payload["trade_id"] in registry_payload


def test_router_blocks_duplicate_trade_id(tmp_path, valid_payload: dict) -> None:
    router = SignalRouter(
        pending_dir=tmp_path / "pending",
        lock_dir=tmp_path / "locks",
        registry_path=tmp_path / "trade_id_registry.json",
    )
    router.send(valid_payload)

    with pytest.raises(RuntimeError, match="Duplicate trade_id blocked"):
        router.send(valid_payload)


def test_router_releases_lock_when_write_fails(tmp_path, monkeypatch, valid_payload: dict) -> None:
    router = SignalRouter(
        pending_dir=tmp_path / "pending",
        lock_dir=tmp_path / "locks",
        registry_path=tmp_path / "trade_id_registry.json",
    )

    class _BrokenFile:
        def __enter__(self):
            raise OSError("disk write fail")

        def __exit__(self, exc_type, exc, tb):
            return False

    original_open = __import__("pathlib").Path.open

    def _broken_open(path_obj, mode="r", *args, **kwargs):
        # Keep lock creation working (mode x), fail only payload write.
        if mode == "x":
            return original_open(path_obj, mode, *args, **kwargs)
        return _BrokenFile()

    monkeypatch.setattr("pathlib.Path.open", _broken_open)

    with pytest.raises(OSError):
        router.send(valid_payload)

    assert not (tmp_path / "locks" / f"{valid_payload['trade_id']}.lock").exists()


def test_router_cleanup_expires_stale_pending_and_orphan_lock(tmp_path, valid_payload: dict) -> None:
    router = SignalRouter(
        pending_dir=tmp_path / "pending",
        lock_dir=tmp_path / "locks",
        registry_path=tmp_path / "trade_id_registry.json",
    )
    path = router.send(valid_payload)
    stale_time = time.time() - 1200
    os.utime(path, (stale_time, stale_time))
    lock_path = tmp_path / "locks" / f"{valid_payload['trade_id']}.lock"
    os.utime(lock_path, (stale_time, stale_time))

    expired = router.cleanup_stale(max_age_seconds=600)

    assert valid_payload["trade_id"] in expired
    assert not path.exists()
    assert not lock_path.exists()
