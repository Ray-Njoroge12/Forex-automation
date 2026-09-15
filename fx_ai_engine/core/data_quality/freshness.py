from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence


@dataclass(frozen=True)
class DataQualityDecision:
    approved: bool
    reason_code: str
    details: str


def _parse_utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_calendar_freshness(
    events: Sequence[Mapping[str, object]],
    *,
    now_utc: datetime | None = None,
    minimum_coverage_days: int = 7,
) -> DataQualityDecision:
    now = now_utc or datetime.now(timezone.utc)
    parsed = [_parse_utc(event.get("datetime_utc")) for event in events]
    dates = [value for value in parsed if value is not None]
    if not dates:
        return DataQualityDecision(False, "CALENDAR_UNAVAILABLE", "no valid calendar events loaded")
    coverage_end = max(dates)
    required_end = now + timedelta(days=max(0, minimum_coverage_days))
    if coverage_end < required_end:
        return DataQualityDecision(
            False,
            "CALENDAR_STALE",
            f"coverage_end={coverage_end.isoformat()} required_end={required_end.isoformat()}",
        )
    return DataQualityDecision(
        True,
        "CALENDAR_FRESH",
        f"coverage_end={coverage_end.isoformat()} valid_events={len(dates)}",
    )


def evaluate_rate_differential_freshness(
    metadata: Mapping[str, object],
    *,
    now_utc: datetime | None = None,
    maximum_age_days: int = 45,
) -> DataQualityDecision:
    now = now_utc or datetime.now(timezone.utc)
    updated = _parse_utc(metadata.get("_last_updated"))
    if updated is None:
        return DataQualityDecision(False, "RATE_DIFFERENTIAL_DATE_MISSING", "_last_updated is absent or invalid")
    age = now - updated
    if age > timedelta(days=max(1, maximum_age_days)):
        return DataQualityDecision(
            False,
            "RATE_DIFFERENTIAL_STALE",
            f"last_updated={updated.isoformat()} age_days={age.total_seconds() / 86400:.1f}",
        )
    return DataQualityDecision(
        True,
        "RATE_DIFFERENTIAL_FRESH",
        f"last_updated={updated.isoformat()} age_days={age.total_seconds() / 86400:.1f}",
    )
