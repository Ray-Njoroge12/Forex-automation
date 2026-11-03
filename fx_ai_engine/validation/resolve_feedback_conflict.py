"""List or resolve execution-feedback conflicts after checking broker evidence.

Unresolved conflicts halt new entries (fail closed). Resolve one only after
comparing it with the account's orders, deals and positions and the EA log.
Resolution records your note as a FEEDBACK_CONFLICT_RESOLVED risk event and
does not change any trade or reservation.

Usage (from fx_ai_engine/):
    python -m validation.resolve_feedback_conflict --list
    python -m validation.resolve_feedback_conflict --id 1 --note "DEMO_MATRIX test payload; EA OrderCheck rejected it, no order or deal exists"
"""
from __future__ import annotations

import argparse
import json

from database.db import list_unresolved_execution_feedback_conflicts, resolve_execution_feedback_conflict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List or resolve execution-feedback conflicts")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--id", type=int)
    parser.add_argument("--note", default="", help="broker evidence you checked (required with --id)")
    args = parser.parse_args(argv)
    if args.list:
        conflicts = list_unresolved_execution_feedback_conflicts()
        print(json.dumps(conflicts, indent=2) if conflicts else "No unresolved conflicts.")
        return 0
    if not resolve_execution_feedback_conflict(args.id, operator_note=args.note):
        print(f"Conflict {args.id} not found or already resolved.")
        return 1
    print(f"Conflict {args.id} resolved; note recorded as a FEEDBACK_CONFLICT_RESOLVED risk event.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
