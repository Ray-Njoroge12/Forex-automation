#!/usr/bin/env python3
"""PreToolUse hook: block MetaTrader 5 MCP trading tools for AI agents.

Invariant 1 (AGENTS.md): only the EA places broker orders. Exit code 2 blocks
the tool call; the message on stderr explains why.
"""
from __future__ import annotations

import json
import re
import sys

TRADE_TOOL = re.compile(r"(^|[^a-z])trade_(send|modify|close|delete)[a-z_]*", re.IGNORECASE)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    names = [str(payload.get(key, "")) for key in ("tool_name", "toolName", "name")]
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if isinstance(tool_input, dict):
        names += [str(tool_input.get(key, "")) for key in ("toolName", "tool_name", "name")]
    if any(TRADE_TOOL.search(name) for name in names if name):
        sys.stderr.write(
            "Blocked: MT5 trading tools are not allowed for AI agents. "
            "Only the FX_Execution EA places orders (AGENTS.md invariant 1).\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
