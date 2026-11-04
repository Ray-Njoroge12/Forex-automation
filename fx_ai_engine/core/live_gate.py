"""Guarded ``--mode live``: startup gate and per-loop halts (ADR 0013).

``startup_gate`` decides whether a process may trade a real account. It returns a
``LiveSession`` or ``None`` (every refusal is persisted as a LIVE_STARTUP_REFUSED
risk event). ``LiveSession.attach`` hooks two methods on one Engine instance so
``check_loop`` runs every iteration, right after ``_update_account_state``. The
loop check only sets ``account_status.is_trading_halted``. It never closes
positions, never places orders and never writes to the bridge folder.

Everything fails closed: unreadable, stale or inconsistent input halts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from config_microcapital import apply_runtime_experiment_config, get_policy_config
from core import live_approval
from core.alerts import alert_risk_halt
from core.evidence import EvidenceContext, build_runtime_evidence_context
from core.health import update_health_status
from core.live_approval import (
    APPROVAL_ENV,
    CONFIRM_ENV,
    DEFAULT_APPROVAL_PATH,
    LIVE_POLICY_MODE,
    LIVE_STRATEGY_ID,
    LiveApproval,
    LiveApprovalError,
)
from core.policy.envelope import canonical_policy_hash
from database import db

logger = logging.getLogger("fx_ai_engine.live_gate")

LOSS_LATCH_KEY = "live_loss_streak_latch"
STATUS_MAX_AGE_SECONDS = 30.0
EQUITY_MAX_AGE_SECONDS = 60.0
FUTURE_TOLERANCE_SECONDS = 5.0
WAIVER_REMINDER_INTERVAL = timedelta(hours=4)
KILL_SWITCH_FILENAME = "KILL_SWITCH"
POLICY_MODE_ENV = "FX_POLICY_MODE"

# Severity of the risk event written when a per-loop code becomes active.
_CRITICAL_CODES = {
    "LIVE_APPROVAL_EXPIRED",
    "LIVE_APPROVAL_CHANGED",
    "LIVE_ACCOUNT_CHANGED",
    "LIVE_LOSS_LIMIT",
    "LIVE_LOSS_STREAK_LATCHED",
    "LIVE_GUARD_MISMATCH",
    "LIVE_GATE_INTERNAL_ERROR",
}
# Codes that stay active for the rest of the process once raised.
_PROCESS_LATCHED_CODES = {"LIVE_LOSS_LIMIT", "LIVE_APPROVAL_CHANGED"}

_ACTIVE_SESSION: "LiveSession | None" = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise LiveApprovalError("LIVE_EA_STATUS_UNAVAILABLE", "generated_at missing")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise LiveApprovalError("LIVE_EA_STATUS_UNAVAILABLE", "generated_at is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise LiveApprovalError("LIVE_EA_STATUS_UNAVAILABLE", "generated_at has no timezone")
    return parsed.astimezone(timezone.utc)


def read_ea_status(bridge_path: Path | str, now: datetime) -> dict[str, Any]:
    """Read ``<bridge>/market/status.json`` (read only). Raises LIVE_EA_STATUS_UNAVAILABLE."""
    path = Path(bridge_path) / "market" / "status.json"
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LiveApprovalError("LIVE_EA_STATUS_UNAVAILABLE", "market/status.json is missing") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise LiveApprovalError("LIVE_EA_STATUS_UNAVAILABLE", f"market/status.json unreadable: {exc}") from exc
    if not isinstance(status, dict) or type(status.get("schema_version")) is not int or status["schema_version"] != 1:
        raise LiveApprovalError("LIVE_EA_STATUS_UNAVAILABLE", "market/status.json has an unsupported schema")
    age = (now - _parse_utc(status.get("generated_at"))).total_seconds()
    if age < -FUTURE_TOLERANCE_SECONDS:
        raise LiveApprovalError("LIVE_EA_STATUS_UNAVAILABLE", f"status timestamp is in the future (age={age:.1f}s)")
    if age > STATUS_MAX_AGE_SECONDS:
        raise LiveApprovalError("LIVE_EA_STATUS_UNAVAILABLE", f"status is stale (age={age:.1f}s)")
    return status


def _same_identity(login: object, server: object, expect_login: int, expect_server: str) -> bool:
    return type(login) is int and login == expect_login and isinstance(server, str) and server == expect_server


def _kill_switch_present(bridge_path: Path) -> tuple[bool, str]:
    """Present when the file exists or cannot be checked. Only 'not found' counts as absent."""
    target = bridge_path / KILL_SWITCH_FILENAME
    try:
        os.stat(target)
    except FileNotFoundError:
        return False, ""
    except OSError as exc:
        return True, f"KILL_SWITCH could not be checked: {exc}"
    return True, f"{KILL_SWITCH_FILENAME} file present"


class LiveSession:
    """State of one approved live run. Created only by ``startup_gate``."""

    def __init__(
        self,
        *,
        approval: LiveApproval,
        approval_sha256: str,
        approval_path: Path,
        evidence_route: str,
        bridge_path: Path,
        policy: Mapping[str, Any],
        evidence_context: EvidenceContext,
        now: datetime,
        clock: Callable[[], datetime] | None = None,
    ):
        self.approval = approval
        self.approval_sha256 = approval_sha256
        self.approval_path = approval_path
        self.evidence_route = evidence_route
        self.bridge_path = Path(bridge_path)
        self.policy = dict(policy)
        self.policy_hash = canonical_policy_hash(policy)
        self.evidence_context = evidence_context
        self.login = approval.account_login
        self.server = approval.account_server
        self.strategy_id = approval.strategy_id
        self._clock = clock or _utcnow
        self.started_at = now
        self.last_waiver_notice = now
        self._active: dict[str, str] = {}
        self._loss_latched = False
        self._approval_changed = False
        self._approval_changed_detail = ""
        self._memory_streak_latch = False
        self.closed = False
        self.bound_bridge: Any = None

    # ------------------------------------------------------------------
    # Lifetime: a session authorizes one run on one bridge object, never a later run
    # ------------------------------------------------------------------
    def close(self) -> None:
        """End the session. ``live_session_covers`` is false from here on."""
        self.closed = True
        self.bound_bridge = None
        _deactivate(self)

    def bind_bridge(self, bridge: Any) -> None:
        """Bind to the bridge object of this run and close when that bridge shuts down."""
        self.bound_bridge = bridge
        original = getattr(bridge, "shutdown", None)
        if not callable(original):
            return
        session = self

        def shutdown_and_close(*args: Any, **kwargs: Any) -> Any:
            try:
                return original(*args, **kwargs)
            finally:
                session.close()

        try:
            bridge.shutdown = shutdown_and_close
        except Exception:
            logger.warning("Could not hook bridge.shutdown; the session stays bound to this bridge object only")

    # ------------------------------------------------------------------
    # Waiver
    # ------------------------------------------------------------------
    @property
    def waiver_active(self) -> bool:
        return self.evidence_route == "waiver"

    def waiver_message(self) -> str:
        return (
            "LIVE EVIDENCE WAIVER IN FORCE: live trading runs without an EVIDENCE_ELIGIBLE verdict. "
            f"reason={self.approval.waiver_reason!r} accepted_by={self.approval.waiver_accepted_by!r} "
            f"account={self.login} approval_expires={self.approval.expires_at_utc.isoformat()}"
        )

    # ------------------------------------------------------------------
    # Engine hooks
    # ------------------------------------------------------------------
    def attach(self, engine: Any) -> bool:
        """Wrap two methods on this engine instance. ``application/**`` is not edited."""
        global _ACTIVE_SESSION
        try:
            if getattr(engine, "_live_gate_session", None) is self:
                return True
            engine_policy = getattr(engine, "policy", None)
            if not isinstance(engine_policy, Mapping) or canonical_policy_hash(engine_policy) != self.policy_hash:
                return self._refuse_attach(engine, "LIVE_POLICY_HASH_MISMATCH", "engine policy hash differs from the approved hash")
            if str(engine_policy.get("MODE_ID", "")).strip().lower() != LIVE_POLICY_MODE:
                return self._refuse_attach(engine, "LIVE_POLICY_MODE_MISMATCH", "engine policy is not micro_live")
            engine_strategy = getattr(engine, "strategy_id", LIVE_STRATEGY_ID)
            if engine_strategy != self.strategy_id:
                return self._refuse_attach(
                    engine, "LIVE_STRATEGY_MISMATCH", f"engine strategy {engine_strategy!r} differs from approval {self.strategy_id!r}"
                )
            if getattr(engine, "use_mock", False):
                return self._refuse_attach(engine, "LIVE_MOCK_FORBIDDEN", "engine runs with the mock bridge")

            original_enforce = engine._enforce_execution_feedback_integrity
            original_loss = engine._record_consecutive_loss_update
            session = self

            def enforce_with_live_checks() -> None:
                original_enforce()
                session.check_loop(engine)

            def record_loss_with_latch(pnl: float) -> None:
                original_loss(pnl)
                session._latch_streak_if_needed(engine)

            engine._enforce_execution_feedback_integrity = enforce_with_live_checks
            engine._record_consecutive_loss_update = record_loss_with_latch
            original_run = getattr(engine, "run", None)
            if callable(original_run):

                def run_then_close(*args: Any, **kwargs: Any) -> Any:
                    try:
                        return original_run(*args, **kwargs)
                    finally:
                        session.close()

                engine.run = run_then_close
            engine._live_gate_session = self
            return True
        except Exception:
            logger.exception("Live gate could not attach to the engine")
            self.close()
            return False

    def _refuse_attach(self, engine: Any, code: str, detail: str) -> bool:
        logger.error("Live gate refused to attach: %s %s", code, detail)
        try:
            db.insert_risk_event(
                "LIVE_STARTUP_REFUSED", "BLOCK", f"{code} {detail}", evidence_context=self.evidence_context
            )
            alert_risk_halt("LIVE_STARTUP_REFUSED", f"{code} {detail}", "BLOCK")
        except Exception:
            logger.exception("Could not persist the attach refusal")
        self.close()
        return False

    # ------------------------------------------------------------------
    # Loss-streak latch
    # ------------------------------------------------------------------
    def _streak_threshold(self) -> int:
        return int(self.policy["LOSS_HALT_THRESHOLD"])

    def _streak_latched(self) -> bool:
        if self._memory_streak_latch:
            return True
        return db.get_engine_runtime_state(LOSS_LATCH_KEY, evidence_context=self.evidence_context) is not None

    def _latch_streak_if_needed(self, engine: Any) -> None:
        try:
            streak = int(engine.account_status.consecutive_losses)
            if streak < self._streak_threshold() or self._streak_latched():
                return
            value = f"latched_at={self._clock().isoformat()} consecutive_losses={streak}"
            try:
                db.set_engine_runtime_state(LOSS_LATCH_KEY, value, evidence_context=self.evidence_context)
            except Exception:
                self._memory_streak_latch = True
                logger.exception("Could not persist the loss-streak latch; holding it in memory")
            engine.account_status.is_trading_halted = True
            logger.critical("Loss streak %s reached the halt threshold; live trading latched until an operator reset", streak)
        except Exception:
            logger.exception("Loss-streak latch failed; halting")
            self._memory_streak_latch = True
            try:
                engine.account_status.is_trading_halted = True
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Per-loop check
    # ------------------------------------------------------------------
    def check_loop(self, engine: Any, now: datetime | None = None) -> list[str]:
        """Evaluate every live condition, halt on any active code, return the active codes."""
        codes: dict[str, str] = {}
        try:
            current = now or self._clock()
            self._evaluate(engine, current, codes)
        except Exception as exc:
            logger.exception("Live gate check failed; halting")
            codes["LIVE_GATE_INTERNAL_ERROR"] = f"{type(exc).__name__}: {exc}"
            current = now or _utcnow()
        # The halt comes first: nothing below may stop it from being applied.
        self._halt(engine, codes)
        # Audit and health side effects fail closed: a failure is itself a halt reason.
        try:
            self._record_transitions(engine, codes)
        except Exception as exc:
            logger.exception("Could not persist live-gate transitions")
            self._internal_error(engine, codes, f"transition persistence failed: {type(exc).__name__}: {exc}")
        try:
            self._waiver_reminder(engine, current)
        except Exception as exc:
            logger.exception("Could not emit the waiver reminder")
            self._internal_error(engine, codes, f"waiver reminder failed: {type(exc).__name__}: {exc}")
        try:
            self._update_health(codes)
        except Exception as exc:
            logger.exception("Could not update live health fields")
            self._internal_error(engine, codes, f"health update failed: {type(exc).__name__}: {exc}")
        return list(codes)

    @staticmethod
    def _halt(engine: Any, codes: Mapping[str, str]) -> None:
        try:
            if codes:
                engine.account_status.is_trading_halted = True
        except Exception:
            logger.exception("Could not set the trading halt flag")

    def _internal_error(self, engine: Any, codes: dict[str, str], detail: str) -> None:
        """Halt on a failed side effect and make a best-effort attempt to record why."""
        first = "LIVE_GATE_INTERNAL_ERROR" not in codes
        codes["LIVE_GATE_INTERNAL_ERROR"] = (
            detail if first else f"{codes['LIVE_GATE_INTERNAL_ERROR']}; {detail}"
        )
        self._halt(engine, codes)
        if first:
            try:
                self._record_transitions(engine, codes)
            except Exception:
                logger.exception("Could not persist the LIVE_GATE_INTERNAL_ERROR transition")

    def _update_health(self, codes: Mapping[str, str]) -> None:
        update_health_status(
            live_mode=True,
            live_evidence_waiver=self.waiver_active,
            live_waiver_reason=self.approval.waiver_reason if self.waiver_active else None,
            live_halt_reasons=list(codes),
            live_approval_expires_at=self.approval.expires_at_utc.isoformat(),
        )

    def _evaluate(self, engine: Any, now: datetime, codes: dict[str, str]) -> None:
        approval = self.approval
        if now >= approval.expires_at_utc:
            codes["LIVE_APPROVAL_EXPIRED"] = f"expired at {approval.expires_at_utc.isoformat()}"
        self._check_approval_file(codes)

        try:
            status = read_ea_status(self.bridge_path, now)
        except LiveApprovalError as exc:
            codes["LIVE_EA_STATUS_UNAVAILABLE"] = exc.detail
        else:
            if not _same_identity(status.get("login"), status.get("server"), self.login, self.server):
                codes["LIVE_ACCOUNT_CHANGED"] = (
                    f"EA reports login={status.get('login')!r} server={status.get('server')!r}, "
                    f"approved login={self.login} server={self.server!r}"
                )
            elif status.get("account_is_demo") is not False:
                codes["LIVE_ACCOUNT_CHANGED"] = f"EA reports account_is_demo={status.get('account_is_demo')!r}"
            try:
                live_approval.check_ea_version(status.get("ea_version"), approval.ea_version_min)
                live_approval.check_live_guard(status, approval)
            except LiveApprovalError as exc:
                codes["LIVE_GUARD_MISMATCH"] = f"{exc.code} {exc.detail}"

        present, detail = _kill_switch_present(self.bridge_path)
        if present:
            codes["LIVE_KILL_SWITCH"] = detail

        self._check_loss(engine, now, codes)

        if self._streak_latched() or self._streak_reached(engine):
            self._latch_streak_if_needed(engine)
            codes["LIVE_LOSS_STREAK_LATCHED"] = "loss streak latch is set; operator reset required"

    def _streak_reached(self, engine: Any) -> bool:
        return int(engine.account_status.consecutive_losses) >= self._streak_threshold()

    def _check_approval_file(self, codes: dict[str, str]) -> None:
        if not self._approval_changed:
            try:
                raw = live_approval.read_approval_bytes(self.approval_path)
            except LiveApprovalError as exc:
                self._approval_changed = True
                self._approval_changed_detail = f"approval file unreadable: {exc.code} {exc.detail}"
            else:
                # A byte-identical file carries the identical policy_hash, so the hash
                # needs no separate re-check while the sha256 is unchanged.
                if hashlib.sha256(raw).hexdigest() != self.approval_sha256:
                    self._approval_changed = True
                    self._approval_changed_detail = "approval file content changed since startup"
        if self._approval_changed:
            codes["LIVE_APPROVAL_CHANGED"] = self._approval_changed_detail

    def _check_loss(self, engine: Any, now: datetime, codes: dict[str, str]) -> None:
        if self._loss_latched:
            codes["LIVE_LOSS_LIMIT"] = "loss limit reached earlier in this run (latched until restart)"
            return
        status = engine.account_status
        equity = status.equity
        updated_at = status.updated_at
        valid = (
            not isinstance(equity, bool)
            and isinstance(equity, (int, float))
            and math.isfinite(equity)
            and equity > 0
            and isinstance(updated_at, datetime)
            and updated_at.tzinfo is not None
            and (now - updated_at).total_seconds() <= EQUITY_MAX_AGE_SECONDS
        )
        if not valid:
            codes["LIVE_EQUITY_STALE"] = (
                f"account equity unavailable or older than {EQUITY_MAX_AGE_SECONDS:.0f}s; loss limit not evaluated"
            )
            return
        loss = self.approval.starting_balance_usd - float(equity)
        if loss + 1e-9 >= self.approval.max_total_loss_usd:
            self._loss_latched = True
            codes["LIVE_LOSS_LIMIT"] = (
                f"loss {loss:.2f} USD reached max_total_loss_usd {self.approval.max_total_loss_usd:.2f} "
                f"(starting {self.approval.starting_balance_usd:.2f}, equity {float(equity):.2f})"
            )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def _record_transitions(self, engine: Any, codes: dict[str, str]) -> None:
        # _active changes only after each event is stored, so a failed write is retried next loop.
        for code, detail in codes.items():
            if code not in self._active:
                severity = "CRITICAL" if code in _CRITICAL_CODES else "BLOCK"
                log = logger.critical if severity == "CRITICAL" else logger.error
                log("LIVE HALT %s: %s", code, detail)
                engine._insert_risk_event(code, severity, detail)
                self._active[code] = detail
        for code in list(self._active):
            if code not in codes and code not in _PROCESS_LATCHED_CODES:
                logger.warning("Live halt cleared: %s", code)
                engine._insert_risk_event("LIVE_HALT_CLEARED", "INFO", f"{code} cleared")
                del self._active[code]

    def _waiver_reminder(self, engine: Any, now: datetime) -> None:
        if not self.waiver_active or now - self.last_waiver_notice < WAIVER_REMINDER_INTERVAL:
            return
        message = self.waiver_message()
        logger.critical(message)
        engine._insert_risk_event("LIVE_EVIDENCE_WAIVER", "CRITICAL", message)
        self.last_waiver_notice = now  # only after the event is stored, so a failure retries next loop


# ----------------------------------------------------------------------
# Startup gate
# ----------------------------------------------------------------------
def _deactivate(session: "LiveSession | None") -> None:
    global _ACTIVE_SESSION
    if session is None or _ACTIVE_SESSION is session:
        _ACTIVE_SESSION = None


def reset_active_session() -> None:
    """Forget the active session (tests, and a clean slate after a refused attach)."""
    global _ACTIVE_SESSION
    _ACTIVE_SESSION = None


def active_session() -> "LiveSession | None":
    return _ACTIVE_SESSION


def live_session_covers(policy: Mapping[str, Any], bridge: Any) -> bool:
    """True only for the policy, bridge object and account the open session was approved for."""
    session = _ACTIVE_SESSION
    if session is None or session.closed:
        return False
    try:
        if bridge is not session.bound_bridge:
            return False
        if session._clock() >= session.approval.expires_at_utc:
            return False
        if str(policy.get("MODE_ID", "")).strip().lower() != LIVE_POLICY_MODE:
            return False
        if canonical_policy_hash(policy) != session.policy_hash:
            return False
        return _same_identity(getattr(bridge, "login", None), getattr(bridge, "server", None), session.login, session.server)
    except Exception:
        return False


def _refuse(
    code: str,
    detail: str,
    context: EvidenceContext | None,
) -> None:
    message = f"{code} {detail}".strip()
    logger.error("Live startup refused: %s", message)
    try:
        db.insert_risk_event("LIVE_STARTUP_REFUSED", "BLOCK", message, evidence_context=context)
    except Exception:
        logger.exception("Could not persist the LIVE_STARTUP_REFUSED risk event")
    try:
        alert_risk_halt("LIVE_STARTUP_REFUSED", message, "BLOCK")
    except Exception:
        logger.exception("Could not send the startup refusal alert")


def startup_gate(
    bridge: Any,
    *,
    policy_mode_arg: str | None,
    use_mock: bool,
    running_strategy_id: str | None = None,
    env: Mapping[str, str] | None = None,
    bridge_path: Path | str | None = None,
    now: datetime | None = None,
) -> LiveSession | None:
    """Return a LiveSession when every live condition holds, else ``None`` after recording why."""
    global _ACTIVE_SESSION
    _ACTIVE_SESSION = None
    source: Mapping[str, str] = os.environ if env is None else env
    current = now or _utcnow()
    context: EvidenceContext | None = None
    try:
        pristine = get_policy_config(LIVE_POLICY_MODE, env={}, apply_overrides=False)
        context = build_runtime_evidence_context(
            pristine,
            use_mock=False,
            login=_safe_int(getattr(bridge, "login", 0)),
            server=str(getattr(bridge, "server", "") or ""),
        )
        session = _run_checks(
            bridge,
            pristine=pristine,
            context=context,
            policy_mode_arg=policy_mode_arg,
            use_mock=use_mock,
            running_strategy_id=running_strategy_id,
            source=source,
            bridge_path=bridge_path,
            now=current,
        )
        if session.waiver_active:
            message = session.waiver_message()
            logger.critical(message)
            db.insert_risk_event("LIVE_EVIDENCE_WAIVER", "CRITICAL", message, evidence_context=context)
            alert_risk_halt("LIVE_EVIDENCE_WAIVER", message, "CRITICAL")
        db.insert_risk_event(
            "LIVE_STARTUP_APPROVED",
            "INFO",
            (
                f"login={session.login} server={session.server} policy_hash={session.policy_hash} "
                f"strategy={session.strategy_id} approved_by={session.approval.approved_by!r} "
                f"expires_at={session.approval.expires_at_utc.isoformat()} evidence_route={session.evidence_route} "
                f"approval_sha256={session.approval_sha256}"
            ),
            evidence_context=context,
        )
    except LiveApprovalError as exc:
        _refuse(exc.code, exc.detail, context)
        return None
    except Exception as exc:
        logger.exception("Live startup gate failed")
        _refuse("LIVE_GATE_INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", context)
        return None
    session.bind_bridge(bridge)
    _ACTIVE_SESSION = session
    return session


def _safe_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _run_checks(
    bridge: Any,
    *,
    pristine: Mapping[str, Any],
    context: EvidenceContext,
    policy_mode_arg: str | None,
    use_mock: bool,
    running_strategy_id: str | None,
    source: Mapping[str, str],
    bridge_path: Path | str | None,
    now: datetime,
) -> LiveSession:
    # 1. policy mode: only micro_live, from the argument and from the environment.
    requested = (policy_mode_arg or "").strip().lower()
    env_mode = str(source.get(POLICY_MODE_ENV, "") or "").strip().lower()
    if (requested and requested != LIVE_POLICY_MODE) or (env_mode and env_mode != LIVE_POLICY_MODE):
        raise LiveApprovalError(
            "LIVE_POLICY_MODE_MISMATCH",
            f"--mode live runs only {LIVE_POLICY_MODE} (argument={requested!r} {POLICY_MODE_ENV}={env_mode!r})",
        )
    # 2. mock
    if use_mock or str(source.get("USE_MT5_MOCK", "") or "").strip() == "1":
        raise LiveApprovalError("LIVE_MOCK_FORBIDDEN", "live mode never runs on the mock bridge")
    # 3. account type
    try:
        is_demo = bridge.is_demo_account()
    except Exception as exc:
        raise LiveApprovalError("LIVE_ACCOUNT_UNVERIFIED", f"account type check failed: {exc}") from exc
    if is_demo is True:
        raise LiveApprovalError("LIVE_ACCOUNT_IS_DEMO", "connected account is a demo account")
    if is_demo is not False:
        raise LiveApprovalError("LIVE_ACCOUNT_UNVERIFIED", "account type could not be verified")

    # 4. running policy, built the way main() builds it.
    running = apply_runtime_experiment_config(
        get_policy_config(mode_id=LIVE_POLICY_MODE, env=source), run_mode="live", env=source
    )
    running_hash = canonical_policy_hash(running)
    pristine_hash = canonical_policy_hash(pristine)

    # 5. approval file
    raw_path = str(source.get(APPROVAL_ENV, "") or "").strip()
    approval_path = Path(raw_path) if raw_path else DEFAULT_APPROVAL_PATH
    approval, approval_sha = live_approval.load_approval(approval_path, now)

    # 6. account identity: approval, bridge and (when readable) the EA status must agree.
    resolved_bridge_path = Path(bridge_path) if bridge_path is not None else None
    if resolved_bridge_path is None:
        attr = getattr(bridge, "bridge_path", None)
        if attr is not None:
            resolved_bridge_path = Path(attr)
        else:
            from core.bridge_utils import get_mt5_bridge_path

            resolved_bridge_path = get_mt5_bridge_path()
    status: dict[str, Any] | None = None
    status_error: LiveApprovalError | None = None
    try:
        status = read_ea_status(resolved_bridge_path, now)
    except LiveApprovalError as exc:
        status_error = exc
    if not _same_identity(getattr(bridge, "login", None), getattr(bridge, "server", None), approval.account_login, approval.account_server):
        raise LiveApprovalError(
            "LIVE_ACCOUNT_MISMATCH",
            f"connected account login={getattr(bridge, 'login', None)!r} server={getattr(bridge, 'server', None)!r} "
            f"differs from approval login={approval.account_login} server={approval.account_server!r}",
        )
    if status is not None and not _same_identity(status.get("login"), status.get("server"), approval.account_login, approval.account_server):
        raise LiveApprovalError(
            "LIVE_ACCOUNT_MISMATCH",
            f"EA status login={status.get('login')!r} server={status.get('server')!r} differs from the approval",
        )

    # 7. policy hash: approval == pristine == running
    if not (approval.policy_hash == pristine_hash == running_hash):
        raise LiveApprovalError(
            "LIVE_POLICY_HASH_MISMATCH",
            f"approval={approval.policy_hash} pristine={pristine_hash} running={running_hash}",
        )
    # 8. strategy
    strategy = running_strategy_id or LIVE_STRATEGY_ID
    if approval.strategy_id != strategy:
        raise LiveApprovalError(
            "LIVE_STRATEGY_MISMATCH", f"approval strategy {approval.strategy_id!r} differs from running {strategy!r}"
        )
    # 9. typed confirmation
    live_approval.check_confirm(source.get(CONFIRM_ENV), approval.account_login)
    # 10. evidence
    route = live_approval.check_evidence(approval)
    # 11. EA status, version and live_guard
    if status_error is not None or status is None:
        raise status_error or LiveApprovalError("LIVE_EA_STATUS_UNAVAILABLE", "status unavailable")
    if status.get("account_is_demo") is True:
        raise LiveApprovalError("LIVE_ACCOUNT_IS_DEMO", "EA status reports a demo account")
    if status.get("account_is_demo") is not False:
        raise LiveApprovalError("LIVE_ACCOUNT_UNVERIFIED", "EA status does not report account_is_demo=false")
    live_approval.check_ea_version(status.get("ea_version"), approval.ea_version_min)
    live_approval.check_live_guard(status, approval)

    return LiveSession(
        approval=approval,
        approval_sha256=approval_sha,
        approval_path=approval_path,
        evidence_route=route,
        bridge_path=resolved_bridge_path,
        policy=running,
        evidence_context=context,
        now=now,
    )
