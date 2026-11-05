"""Missed-cycle counter, burn-in report and gate H1 evaluator over recorded data (temp DB and logs only)."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from validation import burnin_status, cycle_counter, gate_h1

SCHEMA = Path(__file__).parents[1] / "database" / "schema.sql"
WEEK = [date(2026, 10, 26) + timedelta(days=i) for i in range(5)]  # Mon 26 to Fri 30 October 2026 (UK on GMT)
AFTER_WEEK = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
NAIROBI = timedelta(hours=3)
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"]


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def _funnel(conn, when: datetime, stage: str, outcome: str, reason: str, mode: str = "mt5") -> None:
    for sym in SYMBOLS:
        conn.execute(
            "INSERT INTO decision_funnel_events (timestamp, execution_mode, decision_time, symbol, stage, outcome, reason_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (when.isoformat(), mode, when.isoformat(), sym, stage, outcome, reason),
        )


def _all_cycles(conn, day: date, *, skip: tuple[int, ...] = (), blocked: tuple[int, ...] = ()) -> None:
    for i, slot in enumerate(cycle_counter.expected_slots(day)):
        when = slot + timedelta(seconds=4)
        if i in blocked:
            _funnel(conn, when, "HOST", "REJECT", "HOST_GUARD_BLOCK")
        elif i not in skip:
            _funnel(conn, when, "SESSION", "PASS", "SESSION_ACTIVE")


def _daily_check(conn, when: datetime, *, status="PASS", swap=600) -> None:
    conn.execute(
        "INSERT INTO risk_events (timestamp, execution_mode, rule_name, severity, reason) VALUES (?, 'mt5', ?, 'INFO', ?)",
        (when.isoformat(), gate_h1.DAILY_CHECK_RULE,
         f"status={status} failed={'-' if status == 'PASS' else 'power_source'} swap_used_mb={swap} checks=power_source:{status}"),
    )


def _write_log(logs: Path, day: date, drops: int) -> None:
    # 12:00 local = 09:00 UTC, inside the window.
    lines = "".join(f"KK\t1\t12:00:{i:02d}.100\tNetwork\t'5058284077': connection to MetaQuotes-Demo lost\r\n"
                    for i in range(drops))
    lines += "QQ\t0\t12:30:00.000\tNetwork\t'5058284077': authorized on MetaQuotes-Demo\r\n"
    (logs / f"{day:%Y%m%d}.log").write_text(lines, encoding="utf-16")


def _mt5_logs(tmp_path: Path, drops_by_day: dict[date, int]) -> Path:
    logs = tmp_path / "mt5" / "logs"
    logs.mkdir(parents=True)
    for day, drops in drops_by_day.items():
        _write_log(logs, day, drops)
    return logs


PMSET_HEADER = "2026-10-25 08:00:00 +0300 Wake                \tWake from Deep Idle [CDNVA] : due to UserActivity\n"


def _good_week(tmp_path):
    conn = _db(tmp_path)
    for day in WEEK:
        _all_cycles(conn, day)
        _daily_check(conn, cycle_counter.trading_window(day)[0] + timedelta(minutes=2))
    conn.commit()
    return conn, _mt5_logs(tmp_path, {day: 2 for day in WEEK})


def _evaluate(conn, logs, pmset=PMSET_HEADER, now=AFTER_WEEK):
    return gate_h1.evaluate(conn, WEEK[0], WEEK[-1], now=now, pmset_log=pmset, mt5_logs=logs, log_utc_offset=NAIROBI)


# ---------------------------------------------------------------- cycle counter
def test_expected_slots_follow_the_sessions_and_skip_weekends():
    winter = cycle_counter.expected_slots(date(2026, 11, 4))
    assert len(winter) == 56
    assert winter[0] == datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc)
    assert winter[-1] == datetime(2026, 11, 4, 20, 45, tzinfo=timezone.utc)
    summer = cycle_counter.expected_slots(date(2026, 7, 1))
    assert (summer[0].hour, summer[-1].hour, len(summer)) == (6, 19, 56)
    assert cycle_counter.expected_slots(date(2026, 11, 7)) == []  # Saturday
    assert cycle_counter.trading_window(date(2026, 11, 8)) is None


def test_count_cycles_reports_expected_evaluated_blocked_and_missed(tmp_path):
    conn = _db(tmp_path)
    day = date(2026, 11, 4)
    _all_cycles(conn, day, skip=(10,), blocked=(20, 21))
    _funnel(conn, datetime(2026, 11, 4, 9, 0, 4, tzinfo=timezone.utc), "SESSION", "PASS", "SESSION_ACTIVE", mode="mock")
    (result,) = cycle_counter.count_cycles(conn, day, day, now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    assert (result.expected, result.evaluated, result.host_blocked, result.missed, result.complete) == (56, 53, 2, 3, True)


def test_count_cycles_counts_only_due_slots_today(tmp_path):
    conn = _db(tmp_path)
    day = date(2026, 11, 4)
    _all_cycles(conn, day)
    (result,) = cycle_counter.count_cycles(conn, day, day, now=datetime(2026, 11, 4, 8, 3, tzinfo=timezone.utc))
    # 07:00, 07:15, 07:30, 07:45 are due; 08:00 is inside its 5-minute grace.
    assert (result.expected, result.evaluated, result.missed, result.complete) == (4, 4, 0, False)


def test_mock_rows_never_count(tmp_path):
    conn = _db(tmp_path)
    day = date(2026, 11, 4)
    for slot in cycle_counter.expected_slots(day):
        _funnel(conn, slot + timedelta(seconds=4), "SESSION", "PASS", "SESSION_ACTIVE", mode="mock")
    (result,) = cycle_counter.count_cycles(conn, day, day, now=datetime(2026, 11, 5, tzinfo=timezone.utc))
    assert result.evaluated == 0 and result.missed == 56


def test_burnin_status_prints_the_missed_cycle_table(tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager

    from database import connection as db_connection
    from database import db

    path = tmp_path / "burnin.db"

    @contextmanager
    def connection(db_path=None):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(db_connection, "get_conn", connection)
    db.run_all_migrations()
    with connection() as conn:
        _all_cycles(conn, date(2026, 11, 4), skip=(0, 1))
    monkeypatch.setattr(burnin_status, "DB", path)
    monkeypatch.setattr(burnin_status, "_health", lambda: None)
    assert burnin_status.main(["--since", "2026-11-04"]) == 0
    out = capsys.readouterr().out
    assert "M15 decision cycles in the London/NY sessions" in out
    assert "  2026-11-04       56        54            0      2" in out
    assert "Missed cycles in total:" in out


# ---------------------------------------------------------------- parsers
def test_parse_pmset_log_reads_sleep_and_darkwake_in_utc():
    text = (
        "Time stamp                Domain              Message\n"
        "2026-11-05 13:50:49 +0300 DarkWake            \tDarkWake from Deep Idle [CDNP] : due to smc\n"
        "2026-11-05 13:50:58 +0300 Sleep               \tEntering Sleep state due to 'Maintenance Sleep'\n"
        "2026-11-05 13:51:00 +0300 Wake Requests       \t[*process=dasd request=SleepService]\n"
        "2026-11-05 13:51:10 +0300 Wake                \tWake from Deep Idle [CDNVA]\n"
    )
    earliest, events = gate_h1.parse_pmset_log(text)
    assert earliest == datetime(2026, 11, 5, 10, 50, 49, tzinfo=timezone.utc)
    assert [k for _, k in events] == ["DarkWake", "Sleep"]


def test_parse_mt5_drops_converts_local_log_time_to_utc():
    text = "\ufeffFN\t1\t00:05:10.013\tNetwork\t'5058284077': connection to MetaQuotes-Demo lost\r\n"
    assert gate_h1.parse_mt5_drops(text, date(2026, 11, 4), NAIROBI) == [datetime(2026, 11, 3, 21, 5, 10, tzinfo=timezone.utc)]


# ---------------------------------------------------------------- gate H1
def test_five_clean_trading_days_pass(tmp_path):
    conn, logs = _good_week(tmp_path)
    results = _evaluate(conn, logs)
    assert [r.passed for r in results] == [True] * 5, [r.details for r in results]
    assert gate_h1.trailing_streak(results) == 5


@pytest.mark.parametrize("breaker,condition,expected", [
    ("block", "host_check_at_window_start", gate_h1.FAIL),
    ("late", "host_check_at_window_start", gate_h1.FAIL),
    ("swap", "swap_at_window_start", gate_h1.FAIL),
    ("drops", "mt5_drops_in_window", gate_h1.FAIL),
    ("sleep", "sleep_events_in_window", gate_h1.FAIL),
    ("missed", "missed_cycles", gate_h1.FAIL),
    ("no_engine", "host_check_at_window_start", gate_h1.NO_DATA),
    ("no_log", "mt5_drops_in_window", gate_h1.NO_DATA),
    ("pmset_short", "sleep_events_in_window", gate_h1.NO_DATA),
])
def test_each_condition_breaks_the_streak(tmp_path, breaker, condition, expected):
    conn, logs = _good_week(tmp_path)
    friday = WEEK[-1]
    start = cycle_counter.trading_window(friday)[0]
    pmset = PMSET_HEADER
    if breaker in ("block", "late", "swap", "no_engine"):
        conn.execute("DELETE FROM risk_events WHERE timestamp >= ?", (friday.isoformat(),))
        if breaker != "no_engine":
            _daily_check(
                conn,
                start + timedelta(minutes=40 if breaker == "late" else 1),
                status="BLOCK" if breaker == "block" else "PASS",
                swap=2048 if breaker == "swap" else 600,
            )
    elif breaker == "drops":
        _write_log(logs, friday, 7)
    elif breaker == "no_log":
        (logs / f"{friday:%Y%m%d}.log").unlink()
    elif breaker == "sleep":
        pmset += (start + NAIROBI + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S +0300") + " Sleep  \tEntering Sleep state\n"
    elif breaker == "missed":
        conn.execute("DELETE FROM decision_funnel_events WHERE decision_time LIKE ?", (f"{friday.isoformat()}T15:%",))
    elif breaker == "pmset_short":
        pmset = (start + NAIROBI + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S +0300") + " Wake  \tWake\n"
    results = _evaluate(conn, logs, pmset=pmset)
    assert results[-1].conditions[condition] == expected, results[-1].details
    assert not results[-1].passed
    assert gate_h1.trailing_streak(results) == 0
    if breaker != "pmset_short":
        assert all(r.passed for r in results[:-1])


def test_incomplete_day_is_reported_but_not_counted(tmp_path):
    conn, logs = _good_week(tmp_path)
    results = _evaluate(conn, logs, now=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))
    assert not results[-1].complete and not results[-1].passed
    assert gate_h1.trailing_streak(results) == 4


def test_cli_reports_pass_and_json(tmp_path, monkeypatch, capsys):
    conn, logs = _good_week(tmp_path)
    conn.close()
    pmset_file = tmp_path / "pmset.txt"
    pmset_file.write_text(PMSET_HEADER)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return AFTER_WEEK if tz else AFTER_WEEK.replace(tzinfo=None)

    monkeypatch.setattr(gate_h1, "datetime", Frozen)
    args = ["--since", "2026-10-26", "--until", "2026-10-30", "--db", str(tmp_path / "state.db"),
            "--mt5-data", str(tmp_path / "mt5"), "--pmset-log", str(pmset_file), "--log-utc-offset-hours", "3"]
    assert gate_h1.main(args) == 0
    assert "Gate H1: PASS (trailing streak 5 of 5" in capsys.readouterr().out
    assert gate_h1.main(args + ["--json"]) == 0
    assert '"verdict": "PASS"' in capsys.readouterr().out
