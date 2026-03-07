from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from core.schemas import validate_signal_payload

logger = logging.getLogger("fx_ai_engine.signal_router")


@dataclass(frozen=True)
class SignalRouteError(RuntimeError):
    trade_id: str
    pending_written: bool
    detail: str

    def __str__(self) -> str:
        return f"trade_id={self.trade_id} pending_written={self.pending_written} {self.detail}"


@dataclass(frozen=True)
class RouterCleanupResult:
    stale_pending_trade_ids: tuple[str, ...] = ()
    orphan_lock_trade_ids: tuple[str, ...] = ()


class SignalRouter:
    """Writes validated signal payloads atomically and manages lock lifecycle."""

    def __init__(
        self,
        pending_dir: str | Path = "bridge/pending_signals",
        lock_dir: str | Path = "bridge/active_locks",
        registry_path: str | Path | None = None,
    ):
        self.pending_dir = Path(pending_dir)
        self.lock_dir = Path(lock_dir)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = (
            Path(registry_path)
            if registry_path is not None
            else self.lock_dir.parent / "trade_id_registry.json"
        )
        self.quarantine_dir = self.lock_dir.parent / "quarantine"
        self._processed_trade_ids = self._load_registry()

    def _lock_path(self, trade_id: str) -> Path:
        return self.lock_dir / f"{trade_id}.lock"

    def _pending_path(self, trade_id: str) -> Path:
        return self.pending_dir / f"{trade_id}.json"

    def _load_registry(self) -> set[str]:
        if not self.registry_path.exists():
            return set()
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                logger.warning("Trade registry malformed: expected list path=%s", self.registry_path)
                return set()
            return {str(item) for item in payload}
        except json.JSONDecodeError:
            logger.warning("Trade registry JSON malformed path=%s", self.registry_path)
            return set()

    def _persist_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(sorted(self._processed_trade_ids), f, separators=(",", ":"), ensure_ascii=True)
            f.flush()
        tmp.replace(self.registry_path)

    def _is_duplicate_trade(self, trade_id: str) -> bool:
        return (
            trade_id in self._processed_trade_ids
            or self._lock_path(trade_id).exists()
            or self._pending_path(trade_id).exists()
        )

    def create_lock(self, trade_id: str) -> bool:
        lock_path = self._lock_path(trade_id)
        if lock_path.exists():
            logger.warning("Duplicate trade lock exists trade_id=%s", trade_id)
            return False
        try:
            with lock_path.open("x", encoding="utf-8") as f:
                f.write("LOCKED\n")
        except FileExistsError:
            logger.warning("Duplicate trade lock race detected trade_id=%s", trade_id)
            return False
        return True

    def release_lock(self, trade_id: str) -> None:
        lock_path = self._lock_path(trade_id)
        if lock_path.exists():
            lock_path.unlink()

    def _move_to_quarantine(self, path: Path, bucket: str) -> Path:
        target_dir = self.quarantine_dir / bucket
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        path.replace(target)
        return target

    def send(self, payload: dict) -> Path:
        payload = validate_signal_payload(payload)
        trade_id = payload["trade_id"]

        if self._is_duplicate_trade(trade_id):
            raise RuntimeError(f"Duplicate trade_id blocked by registry/lock/pending: {trade_id}")

        if not self.create_lock(trade_id):
            raise RuntimeError(f"Duplicate processing blocked for trade_id={trade_id}")

        final_path = self._pending_path(trade_id)
        tmp_path = final_path.with_suffix(".json.tmp")
        pending_written = False

        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"), ensure_ascii=True)
                f.flush()
            tmp_path.replace(final_path)
            pending_written = True
            self._processed_trade_ids.add(trade_id)
            self._persist_registry()
            logger.info("Signal written atomically trade_id=%s path=%s", trade_id, final_path)
            return final_path
        except Exception as exc:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            if not pending_written:
                self.release_lock(trade_id)
                raise SignalRouteError(
                    trade_id=trade_id,
                    pending_written=False,
                    detail="router failed before the pending signal became visible to MT5",
                ) from exc
            raise SignalRouteError(
                trade_id=trade_id,
                pending_written=True,
                detail="router failed after publish; preserve-first reconciliation required",
            ) from exc

    def clear_signals(self) -> None:
        """Purge all pending signals and locks."""
        for p in self.pending_dir.glob("*.json"):
            p.unlink(missing_ok=True)
        for p in self.lock_dir.glob("*.lock"):
            p.unlink(missing_ok=True)
        logger.info("Cleared all pending signals and active locks.")

    def cleanup_stale(self, max_age_seconds: int = 600) -> RouterCleanupResult:
        """Quarantine stale pending/lock artifacts and return the affected trade_ids."""
        now = time.time()
        stale_pending_trade_ids: list[str] = []
        orphan_lock_trade_ids: list[str] = []

        for p in self.pending_dir.glob("*.json"):
            age = now - p.stat().st_mtime
            if age <= max_age_seconds:
                continue
            trade_id = p.stem
            quarantined = self._move_to_quarantine(p, "stale_pending")
            self.release_lock(trade_id)
            stale_pending_trade_ids.append(trade_id)
            logger.warning(
                "Quarantined stale pending signal trade_id=%s age_seconds=%.1f path=%s",
                trade_id,
                age,
                quarantined,
            )

        for p in self.lock_dir.glob("*.lock"):
            age = now - p.stat().st_mtime
            if age <= max_age_seconds:
                continue
            trade_id = p.stem
            # Keep lock if a pending file for the same trade still exists.
            if self._pending_path(trade_id).exists():
                continue
            quarantined = self._move_to_quarantine(p, "orphan_locks")
            orphan_lock_trade_ids.append(trade_id)
            logger.info(
                "Quarantined orphan lock trade_id=%s age_seconds=%.1f path=%s",
                trade_id,
                age,
                quarantined,
            )

        return RouterCleanupResult(
            stale_pending_trade_ids=tuple(stale_pending_trade_ids),
            orphan_lock_trade_ids=tuple(orphan_lock_trade_ids),
        )
