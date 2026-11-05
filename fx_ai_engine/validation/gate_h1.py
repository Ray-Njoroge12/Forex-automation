"""Gate H1 (host conditions for live trading on this Mac), from recorded data. Read-only.

Usage (from fx_ai_engine/):
    python -m validation.gate_h1                       # last 14 days
    python -m validation.gate_h1 --since 2026-11-09 --json
    python -m validation.gate_h1 --pmset-log saved_pmset_log.txt

Doc: fx_ai_engine/docs/2026-11-04-mac-host-operating-model.md, section 7. For
each weekday whose trading window (London/NY sessions, UTC) has ended:

1. host_check_at_window_start: the engine's ``HOST_GUARD_DAILY_CHECK`` risk event
   was recorded within 15 minutes of the window opening with ``status=PASS``.
   It covers the host-guard rules (power, sleep prevention, App Nap, replay
   jobs, bridge snapshot); memory, disk and MT5 settings are not recorded.
2. swap_at_window_start: ``swap_used_mb`` in that event is below 1500.
3. mt5_drops_in_window: at most 6 "connection to ... lost" lines in the MT5
   terminal logs during the window (log times are local; see --log-utc-offset-hours).
4. sleep_events_in_window: no Sleep or DarkWake entries in ``pmset -g log``
   during the window.
5. missed_cycles: no missed M15 decision cycles (``validation.cycle_counter``).

A condition without data is NO_DATA, which does not pass. H1 is PASS when the
most recent 5 or more completed trading days pass every condition in a row.
Exit code: 0 = PASS, 1 = not yet. Nothing is written.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.host_rules import MT5_DATA, SWAP_WARN_MB, read_utf16, run_command
from validation import cycle_counter

DB = Path(__file__).parents[1] / "database" / "trading_state.db"
PASS, FAIL, NO_DATA = "PASS", "FAIL", "NO_DATA"
REQUIRED_DAYS = 5
MAX_DROPS_PER_WINDOW = 6
WINDOW_START_GRACE = timedelta(minutes=15)
DAILY_CHECK_RULE = "HOST_GUARD_DAILY_CHECK"

_PMSET_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}) (\S+)")
_DROP_LINE = re.compile(r"\t(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\t[^\n]*connection to \S+ lost")


@dataclass
class DayResult:
    day: str
    window_utc: str
    complete: bool
    conditions: dict[str, str] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.complete and all(v == PASS for v in self.conditions.values())


# ----------------------------------------------------------------------
# Parsers (pure)
# ----------------------------------------------------------------------
def parse_pmset_log(text: str) -> tuple[datetime | None, list[tuple[datetime, str]]]:
    """(earliest entry, [(time, 'Sleep'|'DarkWake'), ...]) in UTC."""
    earliest: datetime | None = None
    events: list[tuple[datetime, str]] = []
    for line in text.splitlines():
        m = _PMSET_LINE.match(line)
        if not m:
            continue
        try:
            stamp = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc)
        except ValueError:
            continue
        if earliest is None or stamp < earliest:
            earliest = stamp
        if m.group(2) in ("Sleep", "DarkWake"):
            events.append((stamp, m.group(2)))
    return earliest, events


def parse_mt5_drops(text: str, log_day: date, utc_offset: timedelta) -> list[datetime]:
    """UTC times of connection drops in one MT5 terminal log (times are local to *log_day*)."""
    out = []
    for m in _DROP_LINE.finditer(text):
        local = datetime(log_day.year, log_day.month, log_day.day, int(m.group(1)), int(m.group(2)), int(m.group(3)))
        out.append((local - utc_offset).replace(tzinfo=timezone.utc))
    return out


def parse_daily_check(reason: str) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=(\S+)", reason))


# ----------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------
def _daily_checks(conn: sqlite3.Connection, since: date, until: date) -> dict[date, tuple[datetime, dict[str, str]]]:
    rows = conn.execute(
        "SELECT timestamp, reason FROM risk_events WHERE rule_name = ? AND execution_mode = 'mt5' "
        "AND timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (DAILY_CHECK_RULE, since.isoformat(), (until + timedelta(days=1)).isoformat()),
    ).fetchall()
    out: dict[date, tuple[datetime, dict[str, str]]] = {}
    for stamp, reason in rows:
        parsed = cycle_counter.parse_utc(stamp)
        if parsed is None:
            continue
        out.setdefault(parsed.date(), (parsed, parse_daily_check(reason)))  # earliest of the day
    return out


def _drops_in(mt5_logs: Path, start: datetime, end: datetime, utc_offset: timedelta) -> int | None:
    local_days = {(start + utc_offset).date(), (end + utc_offset).date()}
    found = False
    drops: list[datetime] = []
    for day in sorted(local_days):
        path = mt5_logs / f"{day:%Y%m%d}.log"
        if not path.exists():
            continue
        found = True
        drops += parse_mt5_drops(read_utf16(path), day, utc_offset)
    if not found:
        return None
    return sum(start <= d < end for d in drops)


def evaluate(
    conn: sqlite3.Connection,
    since: date,
    until: date,
    *,
    now: datetime,
    pmset_log: str,
    mt5_logs: Path,
    log_utc_offset: timedelta,
) -> list[DayResult]:
    cycles = {d.day: d for d in cycle_counter.count_cycles(conn, since, until, now=now)}
    checks = _daily_checks(conn, since, until)
    pmset_start, sleeps = parse_pmset_log(pmset_log)
    results = []
    for day in sorted(cycles):
        start, end = cycle_counter.trading_window(day)  # type: ignore[misc]
        res = DayResult(day.isoformat(), f"{start:%H:%M}-{end:%H:%M}", complete=end <= now)

        check = checks.get(day)
        if check is None:
            res.conditions["host_check_at_window_start"] = NO_DATA
            res.conditions["swap_at_window_start"] = NO_DATA
            res.details["host_check_at_window_start"] = "no HOST_GUARD_DAILY_CHECK recorded (engine not running?)"
            res.details["swap_at_window_start"] = "no host check recorded"
        else:
            stamp, fields = check
            late = stamp > start + WINDOW_START_GRACE
            ok = fields.get("status") == "PASS" and not late
            res.conditions["host_check_at_window_start"] = PASS if ok else FAIL
            res.details["host_check_at_window_start"] = (
                f"at {stamp:%H:%M} UTC status={fields.get('status')} failed={fields.get('failed', '-')}"
                + (" (later than 15 min after the window opened)" if late else "")
            )
            try:
                swap = float(fields["swap_used_mb"])
            except (KeyError, ValueError):
                res.conditions["swap_at_window_start"] = NO_DATA
            else:
                res.conditions["swap_at_window_start"] = PASS if 0 <= swap < SWAP_WARN_MB and not late else FAIL
                res.details["swap_at_window_start"] = f"swap_used_mb={swap:.0f}"

        drops = _drops_in(mt5_logs, start, end, log_utc_offset)
        res.conditions["mt5_drops_in_window"] = NO_DATA if drops is None else (PASS if drops <= MAX_DROPS_PER_WINDOW else FAIL)
        res.details["mt5_drops_in_window"] = "no MT5 log for the window" if drops is None else f"drops={drops}"

        if pmset_start is None or pmset_start > start:
            res.conditions["sleep_events_in_window"] = NO_DATA
            res.details["sleep_events_in_window"] = "pmset log does not cover the window"
        else:
            n = sum(start <= t < end for t, _ in sleeps)
            res.conditions["sleep_events_in_window"] = PASS if n == 0 else FAIL
            res.details["sleep_events_in_window"] = f"sleep/darkwake={n}"

        c = cycles[day]
        res.conditions["missed_cycles"] = PASS if c.missed == 0 and c.expected > 0 else FAIL
        res.details["missed_cycles"] = f"expected={c.expected} evaluated={c.evaluated} host_blocked={c.host_blocked} missed={c.missed}"
        results.append(res)
    return results


def trailing_streak(results: list[DayResult]) -> int:
    streak = 0
    for res in reversed([r for r in results if r.complete]):
        if not res.passed:
            break
        streak += 1
    return streak


def _local_offset() -> timedelta:
    return timedelta(seconds=time.localtime().tm_gmtoff)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate H1 host conditions from recorded data (read-only)")
    today = datetime.now(timezone.utc).date()
    parser.add_argument("--since", default=(today - timedelta(days=14)).isoformat())
    parser.add_argument("--until", default=today.isoformat())
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--mt5-data", type=Path, default=MT5_DATA)
    parser.add_argument("--pmset-log", type=Path, help="saved `pmset -g log` output (default: run pmset)")
    parser.add_argument("--log-utc-offset-hours", type=float, default=None,
                        help="offset of the MT5 terminal log times from UTC (default: this Mac's offset)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    offset = timedelta(hours=args.log_utc_offset_hours) if args.log_utc_offset_hours is not None else _local_offset()
    pmset_text = args.pmset_log.read_text(errors="replace") if args.pmset_log else run_command(["pmset", "-g", "log"], timeout=60)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        results = evaluate(
            conn, date.fromisoformat(args.since), date.fromisoformat(args.until),
            now=datetime.now(timezone.utc), pmset_log=pmset_text,
            mt5_logs=args.mt5_data / "logs", log_utc_offset=offset,
        )
    finally:
        conn.close()
    streak = trailing_streak(results)
    verdict = PASS if streak >= REQUIRED_DAYS else "NOT_YET"

    if args.json:
        print(json.dumps({"verdict": verdict, "trailing_streak": streak, "required_days": REQUIRED_DAYS,
                          "days": [asdict(r) | {"passed": r.passed} for r in results]}, indent=2))
    else:
        names = ["host_check_at_window_start", "swap_at_window_start", "mt5_drops_in_window",
                 "sleep_events_in_window", "missed_cycles"]
        for res in results:
            flag = "PASS" if res.passed else ("in progress" if not res.complete else "FAIL")
            print(f"{res.day} window {res.window_utc} UTC: {flag}")
            for name in names:
                print(f"  {res.conditions.get(name, NO_DATA):7} {name}: {res.details.get(name, '')}")
        print(f"\nGate H1: {verdict} (trailing streak {streak} of {REQUIRED_DAYS} completed trading days)")
        print("Not measured here: Mode T discipline, memory/disk/MT5 settings at window start, physical setup (UPS, backup link).")
    return 0 if verdict == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
