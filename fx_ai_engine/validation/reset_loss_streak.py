"""Operator command that clears the live loss-streak halt.

The live gate latches the halt in engine_runtime_state (core.live_gate.LOSS_LATCH_KEY) and
never clears it. This command is the only way to clear it. It needs typed confirmation, records
the operator and reason in a risk event, and refuses while the engine looks alive or when that
cannot be assessed. Every failure leaves the latch in place.

Exit codes: 0 done or nothing to reset, 2 refused or failed.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from config_microcapital import get_policy_config
from core.account_status import AccountStatus
from core.evidence import build_runtime_evidence_context
from core.live_gate import LOSS_LATCH_KEY
from database import connection as db_connection
from database import db

ALIVE_WINDOW_SECONDS = 120.0
PROBE_TIMEOUT_SECONDS = 2.0
RULE_NAME = "LIVE_LOSS_STREAK_RESET"
NOTHING_TO_RESET = "nothing to reset"


def expected_confirmation(login: int) -> str:
    return f"RESET-LOSS-STREAK-{login}"


def default_health_probe() -> Any:
    """Return the /health body, or None when the connection was refused (nothing listens).

    A timeout means a process may still hold the port, so it is indeterminate. Timeouts and
    every other failure raise, which the caller treats as "cannot assess" and refuses.
    """
    url = f"http://127.0.0.1:{int(os.getenv('HEALTH_PORT', '8080'))}/health"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as exc:  # something answered
        return exc.read()
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return None
        raise
    except ConnectionRefusedError:
        return None


def _parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_seconds(value: Any, now: datetime) -> float:
    return (now - _parse_time(value)).total_seconds()


def _probe_says_alive(body: Any, now: datetime) -> bool:
    """True when the health answer shows a live engine or cannot be understood."""
    if body is None:
        return False
    try:
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8")
        payload = json.loads(body) if isinstance(body, str) else body
        heartbeat = payload["last_heartbeat"]
        if heartbeat is None:
            return True
        return _age_seconds(heartbeat, now) < ALIVE_WINDOW_SECONDS
    except Exception:
        return True


def _fail(message: str) -> int:
    print(f"REFUSED: {message}")
    return 2


def _status_from_row(row: dict[str, Any], now: datetime) -> AccountStatus:
    return AccountStatus(
        balance=float(row["balance"]),
        equity=float(row["equity"]),
        daily_loss_percent=float(row["daily_loss_percent"]),
        weekly_loss_percent=float(row["weekly_loss_percent"]),
        drawdown_percent=float(row["drawdown_percent"]),
        open_risk_percent=float(row["open_risk_percent"]),
        open_usd_exposure_count=int(row["open_usd_exposure_count"]),
        consecutive_losses=0,
        is_trading_halted=False,
        updated_at=now,
        peak_equity=float(row["peak_equity"] or 0.0),
        daily_anchor_date=str(row["daily_anchor_date"] or ""),
        daily_anchor_equity=float(row["daily_anchor_equity"] or 0.0),
        weekly_anchor_key=str(row["weekly_anchor_key"] or ""),
        weekly_anchor_equity=float(row["weekly_anchor_equity"] or 0.0),
    )


def _delete_latch(evidence_context: Any) -> None:
    with db_connection.get_conn() as conn:
        conn.execute(
            "DELETE FROM engine_runtime_state WHERE evidence_stream=? AND account_scope=? AND key=?",
            (evidence_context.evidence_stream, evidence_context.account_scope, LOSS_LATCH_KEY),
        )


def main(
    argv: list[str] | None = None,
    *,
    now: datetime | None = None,
    input_fn: Callable[[str], str] = input,
    health_probe: Callable[[], Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Clear the live loss-streak halt (operator action)")
    parser.add_argument("--account-login", type=int, required=True)
    parser.add_argument("--account-server", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--confirm-text", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    operator = args.operator.strip()
    reason = args.reason.strip()
    server = args.account_server.strip()
    if not operator or not reason or not server:
        return _fail("--operator, --reason and --account-server must not be empty")

    required = expected_confirmation(args.account_login)
    confirm = args.confirm_text
    if confirm is None:
        try:
            confirm = input_fn(f"Type {required} to confirm: ")
        except (EOFError, KeyboardInterrupt):
            return _fail("no confirmation given")
    if confirm != required:
        return _fail("confirmation text does not match")

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # The engine must not be running while the latch is cleared.
    probe = health_probe or default_health_probe
    try:
        if _probe_says_alive(probe(), now):
            return _fail("the engine looks alive (health endpoint); stop it before resetting")
    except Exception as exc:
        return _fail(f"cannot assess whether the engine is running ({type(exc).__name__}); stop it and retry")

    try:
        context = build_runtime_evidence_context(
            get_policy_config("micro_live", env={}),
            use_mock=False,
            login=args.account_login,
            server=server,
        )
        latest = db.get_latest_account_metric(
            evidence_stream=context.evidence_stream,
            account_scope=context.account_scope,
        )
        if latest is not None and _age_seconds(latest["timestamp"], now) < ALIVE_WINDOW_SECONDS:
            return _fail("the account was updated in the last 120 s, the engine looks alive")
        latch = db.get_engine_runtime_state(LOSS_LATCH_KEY, evidence_context=context)
        streak = int(latest["consecutive_losses"]) if latest is not None else 0
        if latch is None and streak == 0:
            print(NOTHING_TO_RESET)
            return 0

        db.insert_risk_event(
            RULE_NAME,
            "INFO",
            f"operator={operator} reason={reason} previous_streak={streak} latch={latch if latch is not None else 'none'}",
            evidence_context=context,
        )
        if latest is not None:
            db.insert_account_metrics(_status_from_row(latest, now), evidence_context=context)
        _delete_latch(context)
    except Exception as exc:
        return _fail(f"reset failed ({type(exc).__name__}: {exc}); the latch was left in place")

    print(f"Loss streak reset for {context.account_scope} by {operator}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
