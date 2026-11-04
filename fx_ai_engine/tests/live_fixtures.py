"""Shared builders for the live-gate tests. Everything lives in temp folders; the real
MT5 bridge folder is never touched."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config_microcapital import get_policy_config
from core import live_approval
from core.policy.envelope import canonical_policy_hash
from tests.test_file_bridge_connection import build_bridge

LOGIN = 7001234567
SERVER = "Broker-Live-Cent"
EA_VERSION = "2026.11.04-2"
MICRO_LIVE_HASH = canonical_policy_hash(get_policy_config("micro_live", env={}))
STARTING_BALANCE = 10.0
MAX_TOTAL_LOSS = 3.0
DEFAULT_LIVE_GUARD = {
    "account_real": True,
    "require_demo": False,
    "allow_live": True,
    "max_lot": 0.01,
    "max_orders_per_day": 4,
    "orders_today": 0,
    "kill_switch": False,
}


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------
# Approval and verdict files
# ----------------------------------------------------------------------
def approval_payload(now: datetime, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "approved_by": "Owner",
        "approved_at_utc": (now - timedelta(hours=1)).isoformat(),
        "expires_at_utc": (now + timedelta(days=7)).isoformat(),
        "account_login": LOGIN,
        "account_server": SERVER,
        "policy_mode": "micro_live",
        "policy_hash": MICRO_LIVE_HASH,
        "strategy_id": "trend_pullback_v1",
        "ea_version_min": EA_VERSION,
        "starting_balance_usd": STARTING_BALANCE,
        "max_total_loss_usd": MAX_TOTAL_LOSS,
        "max_orders_per_day": 4,
        "max_lot_per_order": 0.01,
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not _DROP}


_DROP = object()
DROP = _DROP  # pass DROP as an override value to remove a key


def write_approval(path: Path, now: datetime, **overrides: Any) -> str:
    """Write an approval file; return the sha256 of its bytes."""
    write_json(path, approval_payload(now, **overrides))
    return file_sha256(path)


def write_verdict(path: Path, **overrides: Any) -> str:
    """Write an authenticity verdict file; return the sha256 of its bytes."""
    verdict: dict[str, Any] = {
        "verdict": "PASS",
        "evidence_status": "EVIDENCE_ELIGIBLE",
        "policy_hash": MICRO_LIVE_HASH,
    }
    verdict.update(overrides)
    write_json(path, {k: v for k, v in verdict.items() if v is not _DROP})
    return file_sha256(path)


# ----------------------------------------------------------------------
# Bridge folder that looks like a real (non-demo) cent account
# ----------------------------------------------------------------------
def write_status(root: Path, now: datetime, **changes: Any) -> None:
    """Refresh market/status.json to *now* and apply changes (DROP removes a key)."""
    path = root / "market" / "status.json"
    status = json.loads(path.read_text(encoding="utf-8"))
    status["generated_at"] = iso(now - timedelta(seconds=3))
    for key, value in changes.items():
        if value is _DROP:
            status.pop(key, None)
        else:
            status[key] = value
    write_json(path, status)


def write_live_guard(root: Path, now: datetime, **changes: Any) -> None:
    guard = dict(DEFAULT_LIVE_GUARD)
    for key, value in changes.items():
        if value is _DROP:
            guard.pop(key, None)
        else:
            guard[key] = value
    write_status(root, now, live_guard=guard)


def write_account_snapshot(root: Path, now: datetime, **changes: Any) -> None:
    path = root / "feedback" / "account_snapshot.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["timestamp"] = iso(now - timedelta(seconds=4))
    snapshot.update(changes)
    write_json(path, snapshot)


def make_real_bridge(root: Path, now: datetime, *, open_tickets: tuple[int, ...] = ()) -> Path:
    """build_bridge(), then rewrite the account as a real cent account with a live_guard block."""
    build_bridge(root, now, open_tickets=open_tickets)
    write_status(
        root,
        now,
        account_is_demo=False,
        login=LOGIN,
        server=SERVER,
        ea_version=EA_VERSION,
        account_currency="USD",
        live_guard=dict(DEFAULT_LIVE_GUARD),
    )
    write_account_snapshot(
        root,
        now,
        account_is_demo=False,
        login=LOGIN,
        server=SERVER,
        currency="USD",
        balance=STARTING_BALANCE,
        equity=STARTING_BALANCE,
        margin_free=STARTING_BALANCE,
        floating_pnl=0.0,
    )
    return root


class FakeBridge:
    """Minimal stand-in for FileBridgeConnection as seen by the live gate."""

    def __init__(self, login: int, server: str, is_demo: bool | None, bridge_path: Path | None = None):
        self.login = login
        self.server = server
        self._is_demo = is_demo
        self.bridge_path = bridge_path

    def is_demo_account(self):
        if isinstance(self._is_demo, Exception):
            raise self._is_demo
        return self._is_demo


def folder_state(root: Path) -> dict[str, tuple[int, int]]:
    """Listing plus size and mtime of every file and folder, to prove nothing was written."""
    state: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        state[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return state


@dataclass
class Scenario:
    """A fully valid live start: real-account bridge, approval file, confirm text."""

    root: Path
    now: datetime
    bridge_dir: Path
    approval_path: Path
    verdict_path: Path
    env: dict[str, str]
    bridge: FakeBridge
    waiver: bool = False
    startup_kwargs: dict[str, Any] = field(default_factory=dict)

    def rewrite_approval(self, **overrides: Any) -> None:
        self._write_approval(**overrides)

    def _write_approval(self, **overrides: Any) -> None:
        if self.waiver:
            base: dict[str, Any] = {"evidence_waiver": {"reason": "no micro_live replay yet", "accepted_by": "Owner"}}
        else:
            base = {"evidence": {"verdict_path": str(self.verdict_path), "sha256": file_sha256(self.verdict_path)}}
        base.update(overrides)
        write_approval(self.approval_path, self.now, **base)

    def gate(self, **kwargs: Any):
        from core import live_gate

        params: dict[str, Any] = {
            "policy_mode_arg": None,
            "use_mock": False,
            "env": self.env,
            "now": self.now,
        }
        params.update(self.startup_kwargs)
        params.update(kwargs)
        return live_gate.startup_gate(self.bridge, **params)


def make_scenario(tmp_path: Path, *, waiver: bool = False, now: datetime | None = None) -> Scenario:
    now = now or datetime.now(timezone.utc)
    bridge_dir = make_real_bridge(tmp_path / "bridge", now)
    verdict_path = tmp_path / "verdict.json"
    write_verdict(verdict_path)
    scenario = Scenario(
        root=tmp_path,
        now=now,
        bridge_dir=bridge_dir,
        approval_path=tmp_path / "live_approval.json",
        verdict_path=verdict_path,
        env={
            live_approval.APPROVAL_ENV: str(tmp_path / "live_approval.json"),
            live_approval.CONFIRM_ENV: live_approval.expected_confirm_text(LOGIN),
        },
        bridge=FakeBridge(LOGIN, SERVER, False, bridge_dir),
        waiver=waiver,
    )
    scenario._write_approval()
    return scenario
