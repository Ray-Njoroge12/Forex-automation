"""Host tolerance for a laptop host: the engine host guard and the outage alert.

Host guard (operating-model doc, section 6 item 1). At engine start and every
``interval_seconds`` (every ``blocked_interval_seconds`` while blocked) the guard
evaluates the trading-mode rules in ``core.host_rules.guard_checks``. On a BLOCK
the decision cycle refuses new entries (funnel stage ``HOST``, reason
``HOST_GUARD_BLOCK``) and a ``HOST_GUARD_BLOCK`` risk event is persisted; open
positions, feedback and exit processing are not touched. ``HOST_GUARD_RECOVERED``
is persisted when the host passes again. The first evaluation inside the trading
window of each UTC day is persisted as ``HOST_GUARD_DAILY_CHECK`` (INFO) so gate
H1 can read the host state and swap at the start of each window. The guard is an
extra fail-closed entry block; it changes no risk limit and runs before the Hard
Risk Engine, which keeps the final say on anything that is routed.

Outage alert (item 3). One alert through ``core.alerts`` when the account
snapshot or the bar exports are stale for more than two minutes during trading
hours, and one when they recover. Nothing is read or sent when no alert channel
is configured.

Probes are injected; mock mode and tests never evaluate the real host.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from core import alerts
from core import host_rules
from core.filters.session_filter import is_tradeable_session
from core.host_rules import BLOCK, PASS, Check

logger = logging.getLogger("fx_ai_engine.main")

HOST_GUARD_BLOCK = "HOST_GUARD_BLOCK"
HOST_GUARD_RECOVERED = "HOST_GUARD_RECOVERED"
HOST_GUARD_DAILY_CHECK = "HOST_GUARD_DAILY_CHECK"
HOST_GUARD_PROBE_ERROR = "host_probe_error"
HOST_FUNNEL_STAGE = "HOST"

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_BLOCKED_INTERVAL_SECONDS = 30
OUTAGE_STALE_AFTER_SECONDS = 120
M15_SECONDS = 900

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
        enabled: bool = True,
        disabled_reason: str = "",
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        blocked_interval_seconds: float = DEFAULT_BLOCKED_INTERVAL_SECONDS,
        in_trading_window: Callable[[datetime], bool] = is_tradeable_session,
    ):
        self._evaluate = evaluate
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

    def poll(self, now: datetime, record: Recorder) -> HostGuardStatus:
        """Re-evaluate when due (or at the first poll inside today's trading window)."""
        if not self.enabled:
            return self._status
        daily_due = self._in_trading_window(now) and self._daily_check_day != now.date()
        if not (daily_due or self._due(now)):
            return self._status

        try:
            checks = list(self._evaluate(now))  # type: ignore[misc]
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
            self._daily_check_day = now.date()
            record(
                HOST_GUARD_DAILY_CHECK, "INFO",
                f"status={BLOCK if failures else PASS} failed={','.join(failed) or '-'} "
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
    return HostGuard(lambda now: host_rules.guard_checks(bridge=bridge_path, now=now))


# ----------------------------------------------------------------------
# Outage alert
# ----------------------------------------------------------------------
def bridge_freshness(bridge_path: Path, now: datetime) -> dict[str, float | None]:
    """Seconds of staleness per feed; None means missing or unreadable.

    ``account_snapshot``: age of feedback/account_snapshot.json ``timestamp``.
    ``bar_exports``: the larger of the market/status.json ``generated_at`` age and
    the time since the next M15 bar after the oldest ``latest_closed_bars`` entry
    should have closed (the EA writes bar files only on a new bar).
    """
    snapshot = host_rules.snapshot_age_seconds(bridge_path / "feedback" / "account_snapshot.json", now)
    status_path = bridge_path / "market" / "status.json"
    status_age = host_rules.snapshot_age_seconds(status_path, now, key="generated_at")
    bars: float | None = None
    if status_age is not None:
        try:
            latest = json.loads(status_path.read_text())["latest_closed_bars"]
            opens = [int(per_symbol["M15"]) for per_symbol in latest.values()]
            next_close = min(opens) + 2 * M15_SECONDS
            bars = max(status_age, now.timestamp() - next_close)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            bars = None
    return {"account_snapshot": snapshot, "bar_exports": bars}


class OutageMonitor:
    """Two messages per outage: one when a feed is stale > 2 min in trading hours, one on recovery."""

    def __init__(
        self,
        probe: Callable[[datetime], dict[str, float | None]],
        *,
        send: Callable[[str], bool] | None = None,
        is_configured: Callable[[], bool] | None = None,
        stale_after_seconds: float = OUTAGE_STALE_AFTER_SECONDS,
        in_trading_hours: Callable[[datetime], bool] = is_tradeable_session,
    ):
        self._probe = probe
        self._send = send if send is not None else alerts.send_telegram_message
        self._is_configured = is_configured if is_configured is not None else alerts.alerts_configured
        self.stale_after_seconds = float(stale_after_seconds)
        self._in_trading_hours = in_trading_hours
        self.active = False
        self._stale_feeds: tuple[str, ...] = ()

    def poll(self, now: datetime) -> str | None:
        """Returns "OUTAGE" or "RECOVERED" when a message was attempted, else None."""
        if not self._is_configured():
            return None
        ages = self._probe(now)
        stale = tuple(sorted(k for k, age in ages.items() if age is None or age > self.stale_after_seconds))
        if not self.active and stale and self._in_trading_hours(now):
            self.active = True
            self._stale_feeds = stale
            described = ", ".join(
                f"{k} {'missing' if ages[k] is None else f'{ages[k]:.0f}s old'}" for k in stale
            )
            self._deliver(
                "<b>⚠️ BRIDGE OUTAGE</b>\n"
                f"<b>Stale:</b> {described}\n"
                f"<b>Time (UTC):</b> {now.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}\n"
                "Check MT5, the EA and the Mac (power, sleep, network)."
            )
            return "OUTAGE"
        if self.active and not stale:
            self.active = False
            self._deliver(
                "<b>✅ BRIDGE RECOVERED</b>\n"
                f"<b>Feeds:</b> {', '.join(self._stale_feeds)} fresh again\n"
                f"<b>Time (UTC):</b> {now.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}"
            )
            self._stale_feeds = ()
            return "RECOVERED"
        return None

    def _deliver(self, text: str) -> None:
        try:
            if not self._send(text):
                logger.warning("Outage alert was not delivered")
        except Exception:
            logger.exception("Outage alert failed")


def build_outage_monitor(*, use_mock: bool, bridge_path: Path) -> OutageMonitor | None:
    if use_mock:
        return None
    return OutageMonitor(lambda now: bridge_freshness(bridge_path, now))
