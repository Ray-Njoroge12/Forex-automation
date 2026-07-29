from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.account_status import AccountStatus
from main import Engine, _snapshot_age_seconds


def test_snapshot_age_seconds_computes_age() -> None:
    now_utc = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    snapshot = {"timestamp": (now_utc - timedelta(seconds=45)).isoformat()}
    assert _snapshot_age_seconds(snapshot, now_utc=now_utc) == 45.0


def test_snapshot_age_seconds_returns_none_for_invalid_timestamp() -> None:
    assert _snapshot_age_seconds({"timestamp": "bad-ts"}, now_utc=datetime.now(timezone.utc)) is None


def test_record_consecutive_loss_update_persists_immediately(monkeypatch) -> None:
    writes: list[int] = []
    monkeypatch.setattr(
        "main.insert_account_metrics",
        lambda status, *, evidence_context: writes.append(status.consecutive_losses),
    )

    engine = Engine.__new__(Engine)
    engine.account_status = AccountStatus(consecutive_losses=1)
    engine.evidence_context = SimpleNamespace(evidence_stream="x", account_scope="x")
    engine._consecutive_losses_seeded = True
    engine._consecutive_losses_dirty = False

    engine._record_consecutive_loss_update(-5.0)

    assert engine.account_status.consecutive_losses == 2
    assert writes == [2]
    assert engine._consecutive_losses_dirty is False


def test_record_consecutive_loss_update_keeps_dirty_when_checkpoint_fails(monkeypatch) -> None:
    def _raise(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("main.insert_account_metrics", _raise)

    engine = Engine.__new__(Engine)
    engine.account_status = AccountStatus(consecutive_losses=2)
    engine.evidence_context = SimpleNamespace(evidence_stream="x", account_scope="x")
    engine._consecutive_losses_seeded = True
    engine._consecutive_losses_dirty = False

    engine._record_consecutive_loss_update(-1.0)

    assert engine.account_status.consecutive_losses == 3
    assert engine._consecutive_losses_dirty is True


def test_engine_evaluate_symbol_has_no_ai_tp_scaling_hook() -> None:
    source = inspect.getsource(Engine._evaluate_symbol)
    assert "_apply_ai_tp_scaling" not in source
