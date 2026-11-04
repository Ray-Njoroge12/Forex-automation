"""Validate and atomically install economic-calendar or rate-differential data.

Examples:
    python -m validation.update_market_context --calendar /path/to/calendar.json
    python -m validation.update_market_context --rates /path/to/rates.json

This command deliberately does not fetch from an unspecified internet source.
The operator supplies a sourced file; the command enforces shape and freshness
before replacing the runtime artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from core.data_quality.freshness import (
    evaluate_calendar_freshness,
    evaluate_rate_differential_freshness,
)

DATA_DIR = Path(__file__).parents[1] / "data"
REQUIRED_PAIRS = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"}


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write(target: Path, value: object) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def _source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_calendar(source: Path, *, now_utc: datetime | None = None) -> Path:
    payload = _load_json(source)
    if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
        raise ValueError("calendar source must be a JSON list of event objects")
    for index, item in enumerate(payload):
        missing = {"datetime_utc", "currency", "impact", "event"} - set(item)
        if missing:
            raise ValueError(f"calendar event {index} missing fields: {sorted(missing)}")
    if source.name == "calendar.json" and source.parent.name == "market":
        # The EA's list contains only events, so missing currencies cannot prove
        # whether a query returned zero results or failed. Require the paired
        # status from an EA that explicitly certifies a complete refresh.
        try:
            status = _load_json(source.parent / "status.json")
            if not isinstance(status, Mapping) or status.get("calendar_complete") is not True:
                raise ValueError("MT5 calendar query completeness is unverified")
            if status.get("terminal_connected") is not True or status.get("account_is_demo") is not True:
                raise ValueError("MT5 calendar status is not from a connected demo terminal")
            if type(status.get("calendar_high_impact_events")) is not int or status["calendar_high_impact_events"] != len(payload):
                raise ValueError("MT5 calendar event count does not match status")
            stamp = datetime.fromisoformat(str(status["calendar_generated_at"]).replace("Z", "+00:00"))
            current = now_utc or datetime.now(timezone.utc)
            if stamp.tzinfo is None or current.tzinfo is None:
                raise ValueError("MT5 calendar timestamps must be timezone-aware")
            age = (current - stamp).total_seconds()
            if age < -5 or age > 7200:
                raise ValueError("MT5 calendar export is stale or from the future")
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"MT5 calendar completeness status unavailable: {exc}") from exc
    quality = evaluate_calendar_freshness(payload, now_utc=now_utc)
    if not quality.approved:
        raise ValueError(f"{quality.reason_code}: {quality.details}")
    target = DATA_DIR / "economic_calendar.json"
    _atomic_write(target, payload)
    _atomic_write(
        DATA_DIR / "economic_calendar.manifest.json",
        {
            "installed_at": (now_utc or datetime.now(timezone.utc)).isoformat(),
            "source_file": source.name,
            "source_sha256": _source_sha256(source),
            "quality": quality.details,
        },
    )
    return target


def install_rates(source: Path, *, now_utc: datetime | None = None) -> Path:
    payload = _load_json(source)
    if not isinstance(payload, Mapping):
        raise ValueError("rate source must be a JSON object")
    missing = REQUIRED_PAIRS - set(payload)
    if missing:
        raise ValueError(f"rate source missing pairs: {sorted(missing)}")
    quality = evaluate_rate_differential_freshness(payload, now_utc=now_utc)
    if not quality.approved:
        raise ValueError(f"{quality.reason_code}: {quality.details}")
    for pair in REQUIRED_PAIRS:
        float(payload[pair])
    target = DATA_DIR / "rate_differentials.json"
    _atomic_write(target, payload)
    _atomic_write(
        DATA_DIR / "rate_differentials.manifest.json",
        {
            "installed_at": (now_utc or datetime.now(timezone.utc)).isoformat(),
            "source_file": source.name,
            "source_sha256": _source_sha256(source),
            "quality": quality.details,
        },
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and install FX market-context data")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--calendar", type=Path)
    group.add_argument("--rates", type=Path)
    args = parser.parse_args(argv)
    target = install_calendar(args.calendar) if args.calendar else install_rates(args.rates)
    print(f"Installed validated market context: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
