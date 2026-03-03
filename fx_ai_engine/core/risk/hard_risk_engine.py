from __future__ import annotations

from datetime import datetime, timezone

from core.account_status import AccountStatus
from core.types import RiskDecision


class HardRiskEngine:
    """Absolute authority risk gate. Non-negotiable constraints."""

    # Graduated loss streak: reduce risk multiplier before halting entirely.
    # Streak 1-3 → trade at reduced size; streak >= 4 → full halt.
    LOSS_THROTTLE: dict[int, float] = {1: 0.75, 2: 0.50, 3: 0.25}
    LOSS_HALT_THRESHOLD = 4

    def __init__(self):
        self.max_daily_loss = 0.08
        self.max_weekly_loss = 0.15
        self.max_drawdown = 0.20
        self.max_simultaneous_trades = 2
        self.max_combined_exposure = 0.05

    def validate(
        self,
        account_status: AccountStatus,
        proposed_risk_percent: float,
    ) -> RiskDecision:
        now = datetime.now(timezone.utc).isoformat()

        if account_status.is_trading_halted:
            return self._reject("RISK_HALTED", "Trading is halted")

        if account_status.daily_loss_percent >= self.max_daily_loss:
            return self._reject(
                "RISK_DAILY_STOP",
                f"daily_loss_percent={account_status.daily_loss_percent:.4f}",
            )

        if account_status.weekly_loss_percent >= self.max_weekly_loss:
            return self._reject(
                "RISK_WEEKLY_STOP",
                f"weekly_loss_percent={account_status.weekly_loss_percent:.4f}",
            )

        if account_status.drawdown_percent >= self.max_drawdown:
            return self._reject(
                "RISK_DRAWDOWN_STOP",
                f"drawdown_percent={account_status.drawdown_percent:.4f}",
            )

        # Graduated loss streak: halt at threshold, throttle below it.
        losses = account_status.consecutive_losses
        if losses >= self.LOSS_HALT_THRESHOLD:
            return self._reject(
                "RISK_LOSS_STREAK",
                f"consecutive_losses={losses}",
            )
        throttle = self.LOSS_THROTTLE.get(losses, 1.0)
        effective_risk = proposed_risk_percent * throttle

        if account_status.open_positions_count >= self.max_simultaneous_trades:
            return self._reject(
                "RISK_MAX_SIMULTANEOUS",
                f"open_positions_count={account_status.open_positions_count}",
            )

        if account_status.open_risk_percent + effective_risk > self.max_combined_exposure:
            return self._reject(
                "RISK_COMBINED_EXPOSURE",
                (
                    f"open_risk_percent={account_status.open_risk_percent:.4f}, "
                    f"effective={effective_risk:.4f}, max={self.max_combined_exposure:.4f}"
                ),
            )

        return RiskDecision(
            approved=True,
            reason_code="RISK_APPROVED",
            details="Hard risk constraints satisfied",
            timestamp_utc=now,
            risk_throttle_multiplier=throttle,
        )

    def _reject(self, code: str, details: str) -> RiskDecision:
        return RiskDecision(
            approved=False,
            reason_code=code,
            details=details,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )
