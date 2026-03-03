from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ResetState:
    last_daily_reset_date_utc: str | None = None
    last_weekly_reset_iso_utc: str | None = None


class ResetScheduler:
    """UTC reset logic for daily and weekly metrics."""

    def __init__(self, state: ResetState | None = None):
        self.state = state or ResetState()

    def should_reset_daily(self, now_utc: datetime | None = None) -> bool:
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        day_key = now_utc.strftime("%Y-%m-%d")
        if self.state.last_daily_reset_date_utc != day_key:
            self.state.last_daily_reset_date_utc = day_key
            return True
        return False

    def should_reset_weekly(self, now_utc: datetime | None = None) -> bool:
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        iso_year, iso_week, iso_weekday = now_utc.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        # Monday is 1 in ISO calendar
        if iso_weekday == 1 and self.state.last_weekly_reset_iso_utc != week_key:
            self.state.last_weekly_reset_iso_utc = week_key
            return True
        return False
