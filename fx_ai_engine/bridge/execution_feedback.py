from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from core.mt5_bridge import account_unit_scale, usd_from_account_units
from core.schemas import (
    SchemaError,
    validate_account_snapshot,
    validate_execution_feedback,
    validate_trade_exit,
)

logger = logging.getLogger("fx_ai_engine.execution_feedback")


class ExecutionFeedbackReader:
    """Reads and validates execution feedback and account snapshots safely."""

    def __init__(
        self,
        feedback_dir: str | Path = "bridge/feedback",
        exits_dir: str | Path = "bridge/exits",
        *,
        allow_mock_artifacts: bool = True,
    ):
        self.feedback_dir = Path(feedback_dir)
        self.feedback_dir.mkdir(parents=True, exist_ok=True)
        self.exits_dir = Path(exits_dir)
        self.exits_dir.mkdir(parents=True, exist_ok=True)
        self.allow_mock_artifacts = allow_mock_artifacts
        self._lock_retry_attempts = 3
        self._lock_retry_initial_delay_seconds = 0.05
        # None = currency not yet established by a readable snapshot (fail closed).
        self._last_unit_scale: float | None = None

    def _quarantine_invalid(self, path: Path, reason: str) -> Path:
        quarantine_dir = path.parent / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantined = quarantine_dir / f"{path.stem}.{reason}{path.suffix}"
        try:
            path.replace(quarantined)
        except PermissionError:
            logger.warning(
                "Failed to quarantine bridge artifact due to lock source=%s reason=%s",
                path,
                reason,
            )
            return path
        logger.warning("Quarantined invalid bridge artifact source=%s quarantined=%s", path, quarantined)
        return quarantined

    def _read_json(self, path: Path) -> tuple[dict[str, Any] | None, str]:
        """Read JSON with bounded retry on file locks.

        Returns (payload, state) where state is one of:
        - "ok": payload parsed successfully
        - "missing": file missing/vanished during read
        - "locked": file appears locked after retries; defer processing
        - "malformed": JSON parse failed; quarantine candidate
        """
        if not path.exists():
            return None, "missing"

        delay = self._lock_retry_initial_delay_seconds
        max_attempts = self._lock_retry_attempts + 1

        for attempt in range(1, max_attempts + 1):
            if not path.exists():
                return None, "missing"
            try:
                return json.loads(path.read_text(encoding="utf-8")), "ok"
            except json.JSONDecodeError:
                logger.warning("Malformed JSON encountered path=%s", path)
                return None, "malformed"
            except FileNotFoundError:
                return None, "missing"
            except PermissionError:
                if attempt >= max_attempts:
                    logger.warning(
                        "Permission denied reading path=%s after %d attempts; deferring",
                        path,
                        max_attempts,
                    )
                    return None, "locked"
                time.sleep(delay)
                delay *= 2

        return None, "locked"

    def _unit_scale(self) -> float | None:
        """Account-currency to USD multiplier from the sibling account snapshot.

        Caches the last good value. Returns ``None`` (unknown) until a readable
        snapshot has supplied a non-empty currency; callers must not process
        money values while it is unknown. A valid non-empty currency that is not
        a known cent denomination resolves to the identity scale 1.0.
        """
        payload, state = self._read_json(self.feedback_dir / "account_snapshot.json")
        if state == "ok" and isinstance(payload, dict):
            currency = payload.get("currency")
            if isinstance(currency, str) and currency.strip():
                self._last_unit_scale = account_unit_scale(currency)
        return self._last_unit_scale

    def _payload_scale(self, payload: dict[str, Any], scale: float | None) -> float | None:
        """Scale to apply to *payload*, or ``None`` when its units cannot be established.

        The mock feedback simulator (accepted only when ``allow_mock_artifacts`` is
        set) writes dollar values and no currency, so its payloads count as known
        USD when no snapshot currency exists. Real EA artifacts always need a
        snapshot currency.
        """
        if scale is not None:
            return scale
        if self.allow_mock_artifacts and self._is_mock_payload(payload):
            return 1.0
        return None

    @staticmethod
    def _is_mock_payload(payload: dict[str, Any]) -> bool:
        source = str(payload.get("feedback_source", payload.get("snapshot_source", "")) or "").strip().lower()
        return source == "mock_feedback_simulator"

    @staticmethod
    def _scale_profit_loss(payload: dict[str, Any], scale: float) -> dict[str, Any]:
        """Return a copy with profit_loss in USD; the file on disk is never rewritten."""
        value = payload.get("profit_loss")
        if scale == 1.0 or isinstance(value, bool) or not isinstance(value, (int, float)):
            return payload
        converted = dict(payload)
        converted["profit_loss"] = usd_from_account_units(value, scale)
        return converted

    def _should_block_mock_payload(self, payload: dict[str, Any]) -> bool:
        if self.allow_mock_artifacts or not isinstance(payload, dict):
            return False
        return self._is_mock_payload(payload)

    def _warn_deferred(self, count: int, kind: str) -> None:
        if count:
            logger.warning(
                "Deferred %d %s artifact(s): account currency unknown (no readable account_snapshot.json "
                "with a currency); files left in place for the next poll",
                count,
                kind,
            )

    def delete_artifact(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            logger.warning("Failed to delete bridge artifact due to lock source=%s", path)

    def read_execution_feedback(self) -> list[dict[str, Any]]:
        """Validated execution payloads with USD ``profit_loss``; never deletes valid artifacts."""
        return [payload for _, payload in self.collect_execution_feedback()]

    def collect_execution_feedback(self) -> list[tuple[Path, dict[str, Any]]]:
        """Collect validated execution payloads without deleting source files."""
        results: list[tuple[Path, dict[str, Any]]] = []
        scale = self._unit_scale()
        deferred = 0
        for path in sorted(self.feedback_dir.glob("execution_*.json")):
            payload, state = self._read_json(path)
            if payload is None:
                if state in {"missing", "locked"}:
                    continue
                self._quarantine_invalid(path, "malformed")
                continue
            if self._should_block_mock_payload(payload):
                self._quarantine_invalid(path, "mock_source_blocked")
                continue
            try:
                validate_execution_feedback(payload)
            except SchemaError as exc:
                logger.warning("Execution feedback schema invalid path=%s error=%s", path, exc)
                self._quarantine_invalid(path, "schema_invalid")
                continue
            payload_scale = self._payload_scale(payload, scale)
            if payload_scale is None:
                deferred += 1
                continue
            results.append((path, self._scale_profit_loss(payload, payload_scale)))
        self._warn_deferred(deferred, "execution")
        return results

    def consume_execution_feedback(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path, payload in self.collect_execution_feedback():
            results.append(payload)
            self.delete_artifact(path)
        return results

    def collect_trade_exits(self) -> list[tuple[Path, dict[str, Any]]]:
        """Collect validated trade-exit payloads without deleting source files."""
        results: list[tuple[Path, dict[str, Any]]] = []
        scale = self._unit_scale()
        deferred = 0
        for path in sorted(self.exits_dir.glob("exit_*.json")):
            payload, state = self._read_json(path)
            if payload is None:
                if state in {"missing", "locked"}:
                    continue
                self._quarantine_invalid(path, "malformed")
                continue
            if self._should_block_mock_payload(payload):
                self._quarantine_invalid(path, "mock_source_blocked")
                continue
            try:
                validate_trade_exit(payload)
            except SchemaError as exc:
                logger.warning("Trade exit schema invalid path=%s error=%s", path, exc)
                self._quarantine_invalid(path, "schema_invalid")
                continue
            payload_scale = self._payload_scale(payload, scale)
            if payload_scale is None:
                deferred += 1
                continue
            results.append((path, self._scale_profit_loss(payload, payload_scale)))
        self._warn_deferred(deferred, "exit")
        return results

    def consume_trade_exits(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path, payload in self.collect_trade_exits():
            results.append(payload)
            self.delete_artifact(path)
        return results

    def read_account_snapshot(self) -> dict[str, Any] | None:
        path = self.feedback_dir / "account_snapshot.json"
        payload, state = self._read_json(path)
        if payload is None:
            if state == "malformed" and path.exists():
                self._quarantine_invalid(path, "malformed")
            return None
        if self._should_block_mock_payload(payload):
            self._quarantine_invalid(path, "mock_source_blocked")
            return None
        try:
            snapshot = validate_account_snapshot(payload)
        except SchemaError as exc:
            logger.warning("Account snapshot schema invalid path=%s error=%s", path, exc)
            self._quarantine_invalid(path, "schema_invalid")
            return None
        scale = account_unit_scale(snapshot.get("currency"))
        if scale == 1.0:
            return snapshot
        converted = dict(snapshot)
        for key in ("balance", "equity", "margin_free", "floating_pnl"):
            converted[key] = usd_from_account_units(snapshot[key], scale)
        converted["money_unit"] = "USD"
        converted["unit_scale"] = scale
        self._last_unit_scale = scale
        return converted
