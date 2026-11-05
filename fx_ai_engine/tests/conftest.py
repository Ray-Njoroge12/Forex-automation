"""Suite-wide isolation from the real host.

Engines built in tests must not probe this Mac (pmset, pgrep, defaults) or send
alerts. The host guard factory returns a disabled guard; tests that exercise
the guard inject their own ``HostGuard`` with fake probes.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_from_real_host(monkeypatch):
    from application import host_tolerance

    monkeypatch.setattr(
        host_tolerance,
        "build_host_guard",
        lambda **_: host_tolerance.HostGuard.disabled("tests"),
    )
    monkeypatch.delenv("TELEGRAM_ENABLED", raising=False)
