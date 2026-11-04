"""On-disk cache for replay stages that are expensive and deterministic.

Candidate generation depends only on the policy, the input bars, the cost
model used for spread filters, the date range and the strategy code. The cache
key covers all of them: a policy hash, a hash of each input CSV, the cost
model, start/end, ``CACHE_VERSION`` and a fingerprint of the code files in
``CANDIDATE_CODE_PATHS``. Any change is a cache miss.

Entries are gzip-compressed JSON (never pickle, so reading a cache file cannot
run code), written atomically, and carry their key parts in ``meta``; a file
whose meta does not match the key, or that cannot be read, is treated as a miss.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("fx_ai_engine.replay_cache")

CACHE_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Code whose behaviour changes the candidates. Globs relative to fx_ai_engine/.
CANDIDATE_CODE_PATHS: tuple[str, ...] = (
    "core/agents/regime_agent.py",
    "core/agents/technical_agent.py",
    "core/indicators/**/*.py",
    "core/filters/session_filter.py",
    "core/types.py",
    "core/strategy/**/*.py",
    "backtesting/unified_replay.py",
)


@lru_cache(maxsize=None)
def code_fingerprint(root: Path = PROJECT_ROOT, patterns: tuple[str, ...] = CANDIDATE_CODE_PATHS) -> str:
    digest = hashlib.sha256()
    files = sorted({path for pattern in patterns for path in Path(root).glob(pattern) if path.is_file()})
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def file_sha256_16(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def key_parts(kind: str, **parts: Any) -> dict[str, Any]:
    """Canonical (JSON-round-tripped) key parts, including version and code fingerprint."""
    raw = {"kind": kind, "cache_version": CACHE_VERSION, "code": code_fingerprint(), **parts}
    return json.loads(json.dumps(raw, sort_keys=True, default=str))


def cache_key(kind: str, **parts: Any) -> str:
    canonical = json.dumps(key_parts(kind, **parts), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entry_path(cache_dir: Path, kind: str, key: str) -> Path:
    return Path(cache_dir) / kind / f"{key}.json.gz"


def load(cache_dir: Path, kind: str, key: str, meta: Mapping[str, Any]) -> Any | None:
    """Return the cached payload, or ``None`` on a miss, a meta mismatch or an unreadable file."""
    path = _entry_path(cache_dir, kind, key)
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            entry = json.load(handle)
    except (OSError, EOFError, ValueError) as exc:
        logger.warning("Ignoring unreadable replay cache entry %s: %s", path, exc)
        return None
    if not isinstance(entry, dict) or entry.get("meta") != dict(meta) or "payload" not in entry:
        logger.warning("Ignoring replay cache entry %s whose key parts do not match", path)
        return None
    return entry["payload"]


def store(cache_dir: Path, kind: str, key: str, payload: Any, meta: Mapping[str, Any]) -> Path:
    path = _entry_path(cache_dir, kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{key[:16]}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as handle:
            json.dump({"meta": dict(meta), "payload": payload}, handle, sort_keys=True)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path
