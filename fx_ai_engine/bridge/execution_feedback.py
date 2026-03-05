from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.schemas import (
    SchemaError,
    validate_account_snapshot,
    validate_execution_feedback,
    validate_trade_exit,
)

logger = logging.getLogger("fx_ai_engine.execution_feedback")


class ExecutionFeedbackReader:
    """Reads and validates execution feedback and account snapshots safely."""

    def __init__(self, feedback_dir: str | Path = "bridge/feedback", exits_dir: str | Path = "bridge/exits"):
        self.feedback_dir = Path(feedback_dir)
        self.feedback_dir.mkdir(parents=True, exist_ok=True)
        self.exits_dir = Path(exits_dir)
        self.exits_dir.mkdir(parents=True, exist_ok=True)

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Malformed JSON encountered path=%s", path)
            return None

    def read_execution_feedback(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in sorted(self.feedback_dir.glob("execution_*.json")):
            payload = self._read_json(path)
            if not payload:
                continue
            try:
                validate_execution_feedback(payload)
            except SchemaError as exc:
                logger.warning("Execution feedback schema invalid path=%s error=%s", path, exc)
                continue
            results.append(payload)
        return results

    def consume_execution_feedback(self) -> list[dict[str, Any]]:
        results = []
        for path in sorted(self.feedback_dir.glob("execution_*.json")):
            payload = self._read_json(path)
            if not payload:
                continue
            try:
                validate_execution_feedback(payload)
            except SchemaError as exc:
                logger.warning("Execution feedback schema invalid path=%s error=%s", path, exc)
                continue
            results.append(payload)
            path.unlink(missing_ok=True)
        return results

    def consume_trade_exits(self) -> list[dict[str, Any]]:
        results = []
        for path in sorted(self.exits_dir.glob("exit_*.json")):
            payload = self._read_json(path)
            if not payload:
                continue
            try:
                validate_trade_exit(payload)
            except SchemaError as exc:
                logger.warning("Trade exit schema invalid path=%s error=%s", path, exc)
                continue
            results.append(payload)
            path.unlink(missing_ok=True)
        return results

    def read_account_snapshot(self) -> dict[str, Any] | None:
        path = self.feedback_dir / "account_snapshot.json"
        payload = self._read_json(path)
        if not payload:
            return None
        try:
            return validate_account_snapshot(payload)
        except SchemaError as exc:
            logger.warning("Account snapshot schema invalid path=%s error=%s", path, exc)
            return None
