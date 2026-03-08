from __future__ import annotations

import os
import sys
from pathlib import Path


def _should_skip_implicit_env_load() -> bool:
    """Avoid leaking local .env defaults into automated test processes."""
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def load_runtime_env(env_path: str | Path | None = None) -> None:
    """Load .env key/value pairs into process env without overriding existing vars."""
    if env_path is None:
        if _should_skip_implicit_env_load():
            return
        env_path = Path(__file__).resolve().parent.parent / ".env"
    path = Path(env_path)
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
