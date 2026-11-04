"""Build a real ``main.Engine`` on a temporary file bridge (never the live terminal folder)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core.file_bridge_connection import FileBridgeConnection
from core.metrics import init_metrics
from core.observability import init_tracing


def make_file_bridge_engine(bridge_dir: Path, monkeypatch, *, clock: Callable[[], datetime] | None = None):
    import main
    from application import engine_runtime, feedback_service

    monkeypatch.delenv("USE_MT5_MOCK", raising=False)
    monkeypatch.setenv("BRIDGE_BASE_PATH", str(bridge_dir))
    monkeypatch.setattr(engine_runtime, "update_health_status", lambda **_: None)
    monkeypatch.setattr(feedback_service, "alert_trade_exit", lambda *a, **k: None)
    monkeypatch.setattr(feedback_service, "alert_trade_execution", lambda *a, **k: None)
    conn = FileBridgeConnection(bridge_dir, clock=clock or (lambda: datetime.now(timezone.utc)))
    assert conn.connect(retry_count=1), conn.last_error
    return main.Engine(conn, init_tracing("test"), init_metrics(), use_mock=False, run_mode="demo")
