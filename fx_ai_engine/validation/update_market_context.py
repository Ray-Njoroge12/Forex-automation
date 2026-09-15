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
