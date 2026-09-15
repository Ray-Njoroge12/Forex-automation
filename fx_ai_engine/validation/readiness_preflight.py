"""Report whether the local installation is safe to begin demo validation.

This command does not claim trading readiness. It verifies only local evidence
that must be present before a demo-validation period can begin.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from core.data_quality.freshness import (
    evaluate_calendar_freshness,
    evaluate_rate_differential_freshness,
)
from core.filters.calendar_filter import load_calendar
from core.filters.macro_filter import load_rate_differential_metadata

PROJECT_ROOT = Path(__file__).parents[1]


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    approved: bool
    details: str


def _database_schema_check(database_path: Path) -> CheckResult:
    required = {"trades", "trade_reservations", "trade_lifecycle_events"}
    if not database_path.is_file():
        return CheckResult("database_schema", False, f"database missing: {database_path}")
    try:
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as conn:
            present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as exc:
        return CheckResult("database_schema", False, f"database unreadable: {exc}")
    missing = sorted(required - present)
    return CheckResult(
        "database_schema",
        not missing,
        "migration-managed tables present" if not missing else f"missing tables: {missing}",
    )


def _ea_source_check(ea_path: Path) -> CheckResult:
    if not ea_path.is_file():
        return CheckResult("ea_source_guard", False, f"EA source missing: {ea_path}")
    source = ea_path.read_text(encoding="utf-8")
    required_markers = (
        'ExtractJsonDouble(content, "max_spread_pips"',
        "MathMin(MaxSpreadPips, signalMaxSpreadPips)",
        'ExtractJsonString(content, "reservation_id"',
        "input bool RequirePolicyHashPin = true",
        "REJECTED_POLICY_CONFIGURATION",
    )
    missing = [marker for marker in required_markers if marker not in source]
    return CheckResult(
        "ea_source_guard",
        not missing,
        "required envelope guards present in source" if not missing else "missing envelope guard markers",
    )


def run_preflight(project_root: Path = PROJECT_ROOT) -> list[CheckResult]:
    root = Path(project_root)
    data_dir = root / "data"
    calendar = evaluate_calendar_freshness(load_calendar(data_dir / "economic_calendar.json"))
    rates = evaluate_rate_differential_freshness(load_rate_differential_metadata(data_dir / "rate_differentials.json"))
    return [
        CheckResult("calendar_freshness", calendar.approved, f"{calendar.reason_code}: {calendar.details}"),
        CheckResult("rate_differential_freshness", rates.approved, f"{rates.reason_code}: {rates.details}"),
        _database_schema_check(root / "database" / "trading_state.db"),
        _ea_source_check(root / "mt5_ea" / "FX_Execution.mq5"),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check local prerequisites for FX demo validation")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--json", action="store_true", help="Print machine-readable results")
    parser.add_argument("--strict", action="store_true", help="Return nonzero when any check fails")
    args = parser.parse_args(argv)
    results = run_preflight(args.project_root)
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        for result in results:
            status = "PASS" if result.approved else "BLOCK"
            print(f"{status:5} {result.check_id}: {result.details}")
    return 1 if args.strict and any(not result.approved for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
