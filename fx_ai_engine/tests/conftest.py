"""Suite-wide isolation from the real host.

Engines built in tests must not probe this Mac (pmset, pgrep, defaults), send
alerts or touch the real outage-alert state file. The host guard factory returns
a disabled guard (tests that exercise the guard inject their own ``HostGuard``
with fake probes), and the outage state file is moved into the test's tmp dir.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_from_real_host(monkeypatch, tmp_path):
    from application import host_tolerance

    monkeypatch.setattr(host_tolerance, "DEFAULT_OUTAGE_STATE_PATH", tmp_path / "outage_alert_state.json")

    monkeypatch.setattr(
        host_tolerance,
        "build_host_guard",
        lambda **_: host_tolerance.HostGuard.disabled("tests"),
    )
    monkeypatch.delenv("TELEGRAM_ENABLED", raising=False)
