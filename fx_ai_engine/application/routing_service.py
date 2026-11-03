from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping
from uuid import uuid4

from bridge.signal_router import SignalRouteError, SignalRouter
from core.account_status import AccountStatus
from core.policy.envelope import attach_execution_envelope, canonical_policy_hash
from database.db import transition_trade_reservation, try_create_trade_reservation


class ReservationRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class RoutedReservation:
    reservation_id: str
    payload: dict[str, object]


class RoutingService:
    """Atomically reserves portfolio capacity before publishing an executable signal."""

    def __init__(self, router: SignalRouter, policy: Mapping[str, object], *, ttl_seconds: int = 120):
        self.router = router
        self.policy = dict(policy)
        self.ttl_seconds = max(1, int(ttl_seconds))

    def reserve_and_route(
        self,
        payload: Mapping[str, object],
        *,
        account_status: AccountStatus,
    ) -> RoutedReservation:
        now = datetime.now(timezone.utc)
        reservation_id = f"RSV_{uuid4().hex}"
        decision = try_create_trade_reservation(
            reservation_id=reservation_id,
            trade_id=str(payload["trade_id"]),
            symbol=str(payload["symbol"]),
            risk_percent=float(payload["risk_percent"]),
            policy_hash=canonical_policy_hash(self.policy),
            expires_at=(now + timedelta(seconds=self.ttl_seconds)).isoformat(),
            max_positions=int(self.policy["MAX_SIMULTANEOUS_TRADES"]),
            max_combined_exposure=float(self.policy["MAX_COMBINED_EXPOSURE"]),
            broker_open_positions=int(account_status.open_positions_count),
            broker_open_risk=float(account_status.open_risk_percent),
        )
        if not decision["approved"]:
            raise ReservationRejected(str(decision["reason_code"]))
        executable = attach_execution_envelope(
            payload,
            policy=self.policy,
            reservation_id=reservation_id,
            now_utc=now,
            ttl_seconds=self.ttl_seconds,
        )
        try:
            self.router.send(executable)
        except SignalRouteError as exc:
            transition_trade_reservation(
                reservation_id,
                "EXECUTION_UNCERTAIN" if exc.pending_written else "RELEASED",
                "ROUTER_SEND_UNCERTAIN" if exc.pending_written else "ROUTER_SEND_FAILED",
            )
            raise
        except Exception:
            # An unexpected failure may have occurred after publication. Only
            # SignalRouteError can attest that no pending file was written.
            transition_trade_reservation(reservation_id, "EXECUTION_UNCERTAIN", "ROUTER_SEND_UNCERTAIN")
            raise
        transition_trade_reservation(reservation_id, "ROUTED", "ROUTED_TO_MT5")
        return RoutedReservation(reservation_id=reservation_id, payload=executable)
