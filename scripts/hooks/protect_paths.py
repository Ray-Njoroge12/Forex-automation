#!/usr/bin/env python3
"""PreToolUse hook: ask the owner before an agent edits a protected path.

Protected paths are listed in AGENTS.md. The hook prints a Kiro permission
decision of "ask" so the owner confirms the edit; other paths pass silently.
"""
from __future__ import annotations

import json
import sys

PROTECTED = (
    "fx_ai_engine/core/risk/",
    "fx_ai_engine/config_microcapital.py",
    "fx_ai_engine/mt5_ea/",
    "fx_ai_engine/bridge/",
    "SRS_v1.md",
)


def _paths(payload: object) -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.lower() in {"path", "file_path", "filepath", "targetfile"} and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_paths(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_paths(item))
    return found


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0
    hits = [p for p in _paths(payload) if any(marker in p.replace("\\", "/") for marker in PROTECTED)]
    if hits:
        print(json.dumps({
            "hookSpecificOutput": {
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    "Protected path (AGENTS.md): " + ", ".join(sorted(set(hits)))
                    + ". Needs owner approval and a second-model review before merge."
                ),
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
