"""Missed M15 decision cycles per day (operating-model doc, section 6 item 2).

Expected cycles: every M15 slot of a weekday (UTC) whose start lies inside the
London or New York session (``core.filters.session_filter``). The engine runs a
cycle when a new M15 bar closes, a few seconds after the slot start, and checks
the session at that time, so the slot start is the cycle time.

Evaluated cycles: distinct slots with a ``SESSION``/``PASS`` funnel event (the
cycle reached the per-symbol stages). Host-blocked cycles: distinct slots with a
``HOST``/``REJECT``/``HOST_GUARD_BLOCK`` funnel event. Missed = expected minus
evaluated, so a cycle refused by the host guard, halted by risk state, stopped by
the calendar gate, or never run (engine down, Mac asleep) counts as missed.
Exchange holidays are not modelled; a holiday shows as missed cycles.

Read-only over SQLite. Only engine rows (``execution_mode`` ``mt5``) count by
default, so mock smoke runs that share the database are ignored.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from core.filters.session_filter import is_tradeable_session

SLOT = timedelta(minutes=15)
# A slot counts as expected only once the cycle had time to run.
SLOT_GRACE = timedelta(minutes=5)


@dataclass(frozen=True)
class DayCycles:
    day: date
    expected: int
    evaluated: int
    host_blocked: int
    complete: bool  # False for today while slots are still ahead

    @property
    def missed(self) -> int:
        return self.expected - self.evaluated


def expected_slots(day: date) -> list[datetime]:
    """Start times (UTC) of the in-session M15 slots of *day*; empty on weekends."""
    if day.weekday() >= 5:
        return []
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return [start + i * SLOT for i in range(96) if is_tradeable_session(start + i * SLOT)]


def trading_window(day: date) -> tuple[datetime, datetime] | None:
    """[first slot start, last slot end) of *day*, or None when there is no session."""
    slots = expected_slots(day)
    if not slots:
        return None
    return slots[0], slots[-1] + SLOT


def floor_slot(moment: datetime) -> datetime:
    moment = moment.astimezone(timezone.utc)
    return moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)


def parse_utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _slots(conn: sqlite3.Connection, where: str, params: tuple, since: date, until: date) -> set[datetime]:
    rows = conn.execute(
        f"SELECT decision_time FROM decision_funnel_events WHERE {where} AND decision_time >= ? AND decision_time < ?",
        (*params, since.isoformat(), (until + timedelta(days=1)).isoformat()),
    ).fetchall()
    out = set()
    for (value,) in rows:
        parsed = parse_utc(value)
        if parsed is not None:
            out.add(floor_slot(parsed))
    return out


def count_cycles(
    conn: sqlite3.Connection,
    since: date,
    until: date,
    *,
    now: datetime | None = None,
    execution_mode: str = "mt5",
) -> list[DayCycles]:
    """One ``DayCycles`` per weekday from *since* to *until* (inclusive)."""
    now = now or datetime.now(timezone.utc)
    mode_sql, mode_params = ("execution_mode = ?", (execution_mode,)) if execution_mode else ("1 = 1", ())
    evaluated = _slots(conn, f"{mode_sql} AND stage = 'SESSION' AND outcome = 'PASS'", mode_params, since, until)
    blocked = _slots(
        conn,
        f"{mode_sql} AND stage = 'HOST' AND outcome = 'REJECT' AND reason_code = 'HOST_GUARD_BLOCK'",
        mode_params, since, until,
    )
    days: list[DayCycles] = []
    day = since
    while day <= until:
        all_slots = expected_slots(day)
        if all_slots:
            due = [s for s in all_slots if s + SLOT_GRACE <= now]
            due_set = set(due)
            days.append(DayCycles(
                day=day,
                expected=len(due),
                evaluated=len(due_set & evaluated),
                host_blocked=len((due_set & blocked) - evaluated),
                complete=len(due) == len(all_slots),
            ))
        day += timedelta(days=1)
    return days


def format_table(days: list[DayCycles]) -> list[str]:
    lines = [f"  {'day':10} {'expected':>8} {'evaluated':>9} {'host_blocked':>12} {'missed':>6}"]
    for d in days:
        note = "" if d.complete else "  (in progress)"
        lines.append(f"  {d.day.isoformat():10} {d.expected:8} {d.evaluated:9} {d.host_blocked:12} {d.missed:6}{note}")
    return lines
