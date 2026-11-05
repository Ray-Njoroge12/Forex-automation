"""Host tolerance for a laptop host: the engine host guard and the outage alert.

Host guard (operating-model doc, section 6 item 1). At engine start and every
``interval_seconds`` (every ``blocked_interval_seconds`` while blocked) the guard
evaluates the trading-mode rules in ``core.host_rules.guard_checks``. On a BLOCK
the decision cycle refuses new entries (funnel stage ``HOST``, reason
``HOST_GUARD_BLOCK``) and a ``HOST_GUARD_BLOCK`` risk event is persisted; open
positions, feedback and exit processing are not touched. ``HOST_GUARD_RECOVERED``
is persisted when the host passes again. The engine persists host guard events
without a Telegram risk alert: the outage alert below is the one notification
for a stale bridge. The blocked state is kept in ``engine_runtime_state``
(``HOST_GUARD_STATE_KEY``), so the guard of a restarted engine process records
the recovery, and a refused demo start (no fresh demo snapshot) still evaluates
the guard and records the BLOCK before it exits.

The first evaluation inside the trading window of each UTC day runs
``core.host_rules.window_start_checks`` (every rule that can BLOCK in
``validation.host_check`` trading mode) and is persisted as
``HOST_GUARD_DAILY_CHECK`` (INFO) for gate H1. Entries still block only on the
guard rules. The guard is an extra fail-closed entry block; it changes no risk
limit and runs before the Hard Risk Engine, which keeps the final say on
anything that is routed.

Outage alert (item 3). One alert through ``core.alerts`` when the account
snapshot or the bar exports are stale for more than two minutes during trading
hours, and one when they recover. The episode state is a small JSON file kept
under a lock, so the engine and the watchdog (which keeps polling while the
engine is stopped) share it, and a restart during an outage sends nothing new.
Nothing is read or sent when no alert channel is configured. Engine risk events
that describe the same stale feed (``OUTAGE_COVERED_RULES``) are persisted
without their Telegram alert while this alert covers the feed, and the engine's
"started" message is folded into the episode when the engine starts during or
after an outage it did not run through.

Probes are injected; mock mode and tests never evaluate the real host.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from core import alerts
from core import host_rules
from core.filters.session_filter import is_tradeable_session
from core.host_rules import BLOCK, PASS, Check

try:  # POSIX only; without it the state file is still used, unlocked
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("fx_ai_engine.main")

HOST_GUARD_BLOCK = "HOST_GUARD_BLOCK"
HOST_GUARD_RECOVERED = "HOST_GUARD_RECOVERED"
HOST_GUARD_DAILY_CHECK = "HOST_GUARD_DAILY_CHECK"
HOST_GUARD_PROBE_ERROR = "host_probe_error"
HOST_FUNNEL_STAGE = "HOST"
DAILY_CHECK_SCOPE_FULL = "host_check"  # window_start_checks: every rule that can BLOCK in host_check
DAILY_CHECK_SCOPE_GUARD = "guard"  # guard rules only (no window-start evaluator injected)

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_BLOCKED_INTERVAL_SECONDS = 30
OUTAGE_STALE_AFTER_SECONDS = 120
M15_SECONDS = 900
DEFAULT_OUTAGE_STATE_PATH = Path(__file__).resolve().parents[1] / "database" / "outage_alert_state.json"
HOST_GUARD_STATE_KEY = "host_guard_state"
# The engine's six symbols (application.market_data_service.SYMBOLS, pinned by a
# test). Not imported from there so the watchdog does not load pandas.
BAR_EXPORT_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF")
ACCOUNT_SNAPSHOT_FILE = ("feedback/account_snapshot.json", "timestamp")
MARKET_STATUS_FILE = ("market/status.json", "generated_at")
# Engine risk events that describe a stale bridge file, with that file. While the
# file is older than the bridge's 30 s freshness limit and the outage alert covers
# it (an episode is active, or it is trading hours so a stale feed past 2 minutes
# is reported), the event is persisted without its own Telegram alert.
OUTAGE_COVERED_RULES: dict[str, tuple[str, str]] = {
    "STATE_RECONCILIATION_FAILED": ACCOUNT_SNAPSHOT_FILE,
    "STATE_STALE": ACCOUNT_SNAPSHOT_FILE,
    "LIVE_EQUITY_STALE": ACCOUNT_SNAPSHOT_FILE,
    "LIVE_EA_STATUS_UNAVAILABLE": MARKET_STATUS_FILE,
}
# Outage-state flag: the next engine start sends no "started" message (it is
# part of the outage that was already reported).
START_NOTICE_FOLDED = "fold_next_start_notice"

Evaluator = Callable[[datetime], list[Check]]
Recorder = Callable[[str, str, str], None]


@dataclass(frozen=True)
class HostGuardStatus:
    enabled: bool
    blocked: bool
    failed: tuple[str, ...]
    details: str
    evaluated_at: datetime | None


def _swap_mb(checks: Iterable[Check]) -> str:
    for check in checks:
        if check.check_id == "swap_used":
            return check.details.removeprefix("swap_used=").removesuffix(" MB").strip()
    return "n/a"


def format_checks(checks: Iterable[Check]) -> str:
    return ",".join(f"{c.check_id}:{c.status}" for c in checks)


class HostGuard:
    """Stateful, cheap periodic host check; see the module docstring."""

    def __init__(
        self,
        evaluate: Evaluator | None,
        *,
        evaluate_window_start: Evaluator | None = None,
        enabled: bool = True,
        disabled_reason: str = "",
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        blocked_interval_seconds: float = DEFAULT_BLOCKED_INTERVAL_SECONDS,
        in_trading_window: Callable[[datetime], bool] = is_tradeable_session,
    ):
        self._evaluate = evaluate
        self._evaluate_window_start = evaluate_window_start
        self.enabled = bool(enabled and evaluate is not None)
        self.disabled_reason = disabled_reason
        self.interval_seconds = float(interval_seconds)
        self.blocked_interval_seconds = float(blocked_interval_seconds)
        self._in_trading_window = in_trading_window
        self._last_evaluated: datetime | None = None
        self._daily_check_day: date | None = None
        self._status = HostGuardStatus(self.enabled, False, (), disabled_reason or "not evaluated", None)

    @classmethod
    def disabled(cls, reason: str) -> "HostGuard":
        return cls(None, enabled=False, disabled_reason=reason)

    @property
    def status(self) -> HostGuardStatus:
        return self._status

    def _due(self, now: datetime) -> bool:
        if self._last_evaluated is None:
            return True
        interval = self.blocked_interval_seconds if self._status.blocked else self.interval_seconds
        return (now - self._last_evaluated).total_seconds() >= interval

    def restore_blocked(self, failed: tuple[str, ...], details: str) -> None:
        """Start from the blocked state a previous engine process persisted.

        The next poll evaluates the host: a pass records ``HOST_GUARD_RECOVERED``,
        the same failures record nothing new (one BLOCK per episode across restarts).
        """
        if not self.enabled:
            return
        self._status = HostGuardStatus(True, True, tuple(failed), details or "restored blocked state", None)
        self._last_evaluated = None

    def poll(self, now: datetime, record: Recorder, *, force: bool = False) -> HostGuardStatus:
        """Re-evaluate when due, when forced, or at the first poll inside today's trading window."""
        if not self.enabled:
            return self._status
        daily_due = self._in_trading_window(now) and self._daily_check_day != now.date()
        if not (force or daily_due or self._due(now)):
            return self._status

        # At the window start the full set (a superset of the guard rules) is
        # evaluated once; entries still block only on the guard rules.
        full = daily_due and self._evaluate_window_start is not None
        evaluate = self._evaluate_window_start if full else self._evaluate
        try:
            checks = list(evaluate(now))  # type: ignore[misc]
            failures = host_rules.blocking_failures(checks)
        except Exception as exc:  # fail closed: an unreadable host is not a fit host
            logger.exception("Host guard probes failed")
            checks = [Check(HOST_GUARD_PROBE_ERROR, BLOCK, f"{type(exc).__name__}: {exc}")]
            failures = checks
        failed = tuple(c.check_id for c in failures)
        details = (
            "; ".join(f"{c.check_id}={c.status} {c.details}" for c in failures)
            if failures else "all host guard rules pass"
        )
        previous = self._status
        self._status = HostGuardStatus(True, bool(failures), failed, details, now)
        self._last_evaluated = now

        if failures and (not previous.blocked or previous.failed != failed):
            record(
                HOST_GUARD_BLOCK, "BLOCK",
                f"{HOST_GUARD_BLOCK} new entries refused; open positions and exits untouched: {details}",
            )
            logger.warning("Host guard BLOCK: %s", details)
        elif not failures and previous.blocked:
            record(HOST_GUARD_RECOVERED, "INFO", f"{HOST_GUARD_RECOVERED} host passes again; entries allowed")
            logger.info("Host guard recovered")
        elif previous.evaluated_at is None:
            logger.info("Host guard PASS at start: %s", format_checks(checks))

        if daily_due:
            # The record reports every BLOCK among the evaluated rules, not only
            # the ones that refuse entries: gate H1 needs the full host_check.
            self._daily_check_day = now.date()
            blocks = host_rules.any_blocks(checks)
            record(
                HOST_GUARD_DAILY_CHECK, "INFO",
                f"status={BLOCK if blocks else PASS} "
                f"scope={DAILY_CHECK_SCOPE_FULL if full else DAILY_CHECK_SCOPE_GUARD} "
                f"failed={','.join(c.check_id for c in blocks) or '-'} "
                f"swap_used_mb={_swap_mb(checks)} checks={format_checks(checks)}",
            )
        return self._status


def host_guard_disabled_reason(*, use_mock: bool, run_mode: str, platform: str = sys.platform) -> str:
    """Empty string when the guard applies; otherwise why it is off."""
    if use_mock:
        return "mock mode"
    if run_mode not in ("demo", "live"):
        return f"run mode {run_mode}"
    if platform != "darwin":
        return f"host rules are macOS-specific (platform={platform})"
    return ""


def build_host_guard(*, use_mock: bool, run_mode: str, bridge_path: Path) -> HostGuard:
    reason = host_guard_disabled_reason(use_mock=use_mock, run_mode=run_mode)
    if reason:
        logger.info("Host guard disabled: %s", reason)
        return HostGuard.disabled(reason)
    return HostGuard(
        lambda now: host_rules.guard_checks(bridge=bridge_path, now=now),
        evaluate_window_start=lambda now: host_rules.window_start_checks(bridge=bridge_path, now=now),
    )


def restore_guard_state(guard: HostGuard, repository, evidence_context) -> None:
    """Seed *guard* with the blocked state the last engine process persisted (if any)."""
    if not guard.enabled:
        return
    try:
        raw = repository.get_engine_runtime_state(HOST_GUARD_STATE_KEY, evidence_context=evidence_context)
        data = json.loads(raw) if raw else None
    except Exception:
        logger.warning("Host guard state unreadable; starting unblocked", exc_info=True)
        return
    if isinstance(data, dict) and data.get("blocked") is True:
        failed = tuple(str(f) for f in data.get("failed") or ())
        guard.restore_blocked(failed, str(data.get("details") or ""))
        logger.info("Host guard restored blocked state: %s", ",".join(failed) or "-")


def save_guard_state(guard: HostGuard, repository, evidence_context) -> None:
    status = guard.status
    value = json.dumps({
        "blocked": status.blocked,
        "failed": list(status.failed),
        "details": status.details,
        "evaluated_at": status.evaluated_at.isoformat() if status.evaluated_at else None,
    })
    try:
        repository.set_engine_runtime_state(HOST_GUARD_STATE_KEY, value, evidence_context=evidence_context)
    except Exception:
        logger.warning("Host guard state not persisted; a restart may miss the recovery event", exc_info=True)


def persisting_recorder(guard: HostGuard, insert: Recorder, repository, evidence_context) -> Recorder:
    """Persist the risk event, then the guard state on each BLOCK or recovery."""

    def record(rule: str, severity: str, reason: str) -> None:
        insert(rule, severity, reason)
        if rule in (HOST_GUARD_BLOCK, HOST_GUARD_RECOVERED):
            save_guard_state(guard, repository, evidence_context)

    return record


# ----------------------------------------------------------------------
# Outage alert
# ----------------------------------------------------------------------
_last_bar_cache: dict[Path, tuple[int, int, int]] = {}  # path -> (mtime_ns, size, last bar open)


def last_m15_bar_open(path: Path) -> int | None:
    """Open time (epoch s) of the last bar in an EA bar export; None if missing or unreadable.

    Parsed only when the file's mtime or size changes, so polling every few
    seconds does not re-read six bar files.
    """
    try:
        stat = path.stat()
        cached = _last_bar_cache.get(path)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
        payload = json.loads(path.read_text())
        last_open = payload["bars"][-1][0]
        if type(last_open) is not int or last_open <= 0:
            return None
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    # A file modified within the last second may be rewritten again with the same
    # mtime and size (coarse timestamps); cache it only once that window has passed.
    if time.time() - stat.st_mtime_ns / 1e9 > 1.0:
        _last_bar_cache[path] = (stat.st_mtime_ns, stat.st_size, last_open)
    return last_open


def bridge_freshness(bridge_path: Path, now: datetime) -> dict[str, float | None]:
    """Seconds of staleness per feed; None means missing or unreadable.

    ``account_snapshot``: age of feedback/account_snapshot.json ``timestamp``.
    ``bar_exports``: the larger of the market/status.json ``generated_at`` age
    (the EA heartbeat) and, over the M15 bar files of every engine symbol, the
    time since the bar after each file's last bar should have closed. The EA
    writes a bar file on each new bar, so a file that failed to export goes stale
    here even while status.json keeps advancing. Any missing or unreadable file
    makes the feed None.
    """
    snapshot = host_rules.snapshot_age_seconds(bridge_path / Path(ACCOUNT_SNAPSHOT_FILE[0]), now, ACCOUNT_SNAPSHOT_FILE[1])
    status_age = host_rules.snapshot_age_seconds(bridge_path / Path(MARKET_STATUS_FILE[0]), now, MARKET_STATUS_FILE[1])
    bars: float | None = status_age
    for symbol in BAR_EXPORT_SYMBOLS:
        if bars is None:
            break
        last_open = last_m15_bar_open(bridge_path / "market" / f"bars_{symbol}_M15.json")
        bars = None if last_open is None else max(bars, now.timestamp() - (last_open + 2 * M15_SECONDS))
    return {"account_snapshot": snapshot, "bar_exports": bars}


class OutageState:
    """Outage episode state shared by every process that polls (engine, watchdog).

    A JSON file replaced atomically, guarded by an exclusive lock on a sibling
    ``.lock`` file. When the file cannot be read or written, the last state this
    process saw is used, so a broken disk never turns into repeated alerts.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._memory: dict = {}

    @contextmanager
    def locked(self) -> Iterator[None]:
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path.with_suffix(".lock"), "a")
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError:
            logger.warning("Outage state lock unavailable at %s; using this process's state", self.path)
        try:
            yield
        finally:
            if handle is not None:
                handle.close()  # closing the file releases the lock

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return dict(self._memory)
        except (OSError, ValueError):
            logger.warning("Outage state unreadable at %s; using this process's state", self.path)
            return dict(self._memory)
        if isinstance(data, dict):
            self._memory = data
        return dict(self._memory)

    def save(self, data: dict) -> None:
        self._memory = dict(data)
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError:
            logger.warning("Outage state not written to %s; keeping it in memory", self.path)


class OutageMonitor:
    """Two messages per outage: one when a feed is stale > 2 min in trading hours, one on recovery.

    ``reports`` tells the engine which of its own risk alerts this one replaces,
    and ``claim_start_notice`` whether an engine start may announce itself. ``send``, ``is_configured`` and ``in_trading_hours`` default to
    ``core.alerts.send_telegram_message``, ``core.alerts.alerts_configured`` and
    ``is_tradeable_session``, looked up at each poll.
    """

    def __init__(
        self,
        probe: Callable[[datetime], dict[str, float | None]],
        *,
        state: OutageState,
        send: Callable[[str], bool] | None = None,
        is_configured: Callable[[], bool] | None = None,
        stale_after_seconds: float = OUTAGE_STALE_AFTER_SECONDS,
        in_trading_hours: Callable[[datetime], bool] | None = None,
        bridge_path: Path | None = None,
        engine_process: bool = False,
    ):
        self._probe = probe
        self.state = state
        self._send = send
        self._is_configured = is_configured
        self.stale_after_seconds = float(stale_after_seconds)
        self._in_trading_hours = in_trading_hours
        # Where OUTAGE_COVERED_RULES files are read; None: reports() is always False.
        self.bridge_path = bridge_path
        # True for the monitor inside a running engine: an episode it ran through
        # needs no folded start notice, so it clears the flag.
        self.engine_process = engine_process
        self.active = False

    def _configured(self) -> bool:
        is_configured = self._is_configured if self._is_configured is not None else alerts.alerts_configured
        return bool(is_configured())

    def _in_hours(self, now: datetime) -> bool:
        return bool((self._in_trading_hours if self._in_trading_hours is not None else is_tradeable_session)(now))

    def reports(self, rule_name: str, now: datetime) -> bool:
        """True when the risk event *rule_name* describes a stale feed this alert reports.

        The event's file must be missing or older than the bridge's 30 s limit, and
        an episode must be active or it must be trading hours (a stale feed past
        2 minutes is then reported; one that recovers sooner is not an outage).
        """
        target = OUTAGE_COVERED_RULES.get(rule_name)
        if target is None or self.bridge_path is None or not self._configured():
            return False
        age = host_rules.snapshot_age_seconds(self.bridge_path / Path(target[0]), now, target[1])
        if age is not None and age <= host_rules.SNAPSHOT_FRESH_SECONDS:
            return False
        return self._in_hours(now) or bool(self.state.load().get("active"))

    def claim_start_notice(self) -> bool:
        """Whether an engine start may send its "started" message.

        False while an outage episode is active, and once after an episode that
        the engine did not run through (the restart is part of that outage).
        """
        if not self._configured():
            return True
        with self.state.locked():
            current = self.state.load()
            if current.get("active"):
                return False
            if current.get(START_NOTICE_FOLDED):
                self.state.save({**current, START_NOTICE_FOLDED: False})
                return False
        return True

    def poll(self, now: datetime) -> str | None:
        """Returns "OUTAGE" or "RECOVERED" when a message was attempted, else None."""
        if not self._configured():
            return None
        ages = self._probe(now)
        stale = tuple(sorted(k for k, age in ages.items() if age is None or age > self.stale_after_seconds))
        in_hours = self._in_hours(now)
        # Decide and persist under the lock; deliver after releasing it.
        with self.state.locked():
            current = self.state.load()
            active = bool(current.get("active"))
            if not active and stale and in_hours:
                event = "OUTAGE"
                self.state.save({"active": True, "stale_feeds": list(stale), "since": now.isoformat(),
                                 START_NOTICE_FOLDED: True})
            elif active and not stale:
                event = "RECOVERED"
                feeds = [str(f) for f in current.get("stale_feeds") or []]
                fold = bool(current.get(START_NOTICE_FOLDED)) and not self.engine_process
                self.state.save({"active": False, "stale_feeds": feeds, "recovered_at": now.isoformat(),
                                 START_NOTICE_FOLDED: fold})
            else:
                if not active and self.engine_process and current.get(START_NOTICE_FOLDED):
                    self.state.save({**current, START_NOTICE_FOLDED: False})
                self.active = active
                return None
        self.active = event == "OUTAGE"
        stamp = f"{now.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}"
        if event == "OUTAGE":
            described = ", ".join(
                f"{k} {'missing' if ages[k] is None else f'{ages[k]:.0f}s old'}" for k in stale
            )
            self._deliver(
                "<b>⚠️ BRIDGE OUTAGE</b>\n"
                f"<b>Stale:</b> {described}\n"
                f"<b>Time (UTC):</b> {stamp}\n"
                "Check MT5, the EA and the Mac (power, sleep, network)."
            )
        else:
            self._deliver(
                "<b>✅ BRIDGE RECOVERED</b>\n"
                f"<b>Feeds:</b> {', '.join(feeds) or 'bridge'} fresh again\n"
                f"<b>Time (UTC):</b> {stamp}"
            )
        return event

    def _deliver(self, text: str) -> None:
        try:
            send = self._send if self._send is not None else alerts.send_telegram_message
            if not send(text):
                logger.warning("Outage alert was not delivered")
        except Exception:
            logger.exception("Outage alert failed")


def build_outage_monitor(
    *, use_mock: bool, bridge_path: Path, state_path: Path | None = None, engine_process: bool = False
) -> OutageMonitor | None:
    """The production monitor (engine, engine start and watchdog); None in mock mode."""
    if use_mock:
        return None
    return OutageMonitor(
        lambda now: bridge_freshness(bridge_path, now),
        state=OutageState(state_path or DEFAULT_OUTAGE_STATE_PATH),
        bridge_path=bridge_path,
        engine_process=engine_process,
    )
