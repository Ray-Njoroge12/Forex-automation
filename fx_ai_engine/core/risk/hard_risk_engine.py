from __future__ import annotations

import os
from datetime import datetime, timezone

from core.account_status import AccountStatus
from core.types import RiskDecision


def _read_fixed_risk_usd() -> float | None:
    """Return FIXED_RISK_USD from env, or None if not set / invalid."""
    raw = os.getenv("FIXED_RISK_USD", "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError:
        return None


class HardRiskEngine:
    """Absolute authority risk gate. Non-negotiable constraints.

    Fixed-USD mode (FIXED_RISK_USD env set)
    ----------------------------------------
    Daily / weekly stops are converted from percentages to equivalent dollar
    amounts so they remain meaningful at micro-risk levels.

    Example with FIXED_RISK_USD=10, balance=100,000:
      daily_stop_usd  = 100,000 × 0.08 = $8,000  (SRS limit still respected)
      combined ceiling = $10 × 2        = $20

    The percentage thresholds remain unchanged — only the risk_percent
    supplied by the PortfolioManager is compared against them.

    Loss-Streak Throttle (both modes)
    ----------------------------------
    Streak 1 → 75% of base risk
    Streak 2 → 50%
    Streak 3 → 25%
    Streak >= 4 → full halt
    """

    # Graduated loss streak: reduce risk multiplier before halting entirely.
    LOSS_THROTTLE: dict[int, float] = {1: 0.75, 2: 0.50, 3: 0.25}
    LOSS_HALT_THRESHOLD = 4

    def __init__(self):
        # SRS v1 hard limits (percentage of equity) — never changed without SRS update.
        self.max_daily_loss = 0.08       # 8%
        self.max_weekly_loss = 0.15      # 15%
        self.max_drawdown = 0.20         # 20%
        self.max_simultaneous_trades = 2
        self.max_combined_exposure = 0.05  # 5% — used in pct mode

        # Fixed-USD mode: ceiling is 2x per-trade risk as a fraction (set dynamically).
        self._fixed_risk_usd = _read_fixed_risk_usd()

    def _combined_exposure_limit(self, balance: float) -> float:
        """Return the combined exposure ceiling as a fraction of balance.

        Fixed-USD mode: 2 × fixed_risk / balance.
        Percentage mode: self.max_combined_exposure (5%).
        """
        if self._fixed_risk_usd is not None and balance > 0:
            return round((self._fixed_risk_usd * 2) / balance, 8)
        return self.max_combined_exposure

    def validate(
        self,
        account_status: AccountStatus,
        proposed_risk_percent: float,
    ) -> RiskDecision:
        now = datetime.now(timezone.utc).isoformat()

        # --- Hard halt checks (order matters — most severe first) ---

        if account_status.is_trading_halted:
            return self._reject("RISK_HALTED", "Trading is halted")

        if account_status.daily_loss_percent >= self.max_daily_loss:
            return self._reject(
                "RISK_DAILY_STOP",
                f"daily_loss_percent={account_status.daily_loss_percent:.4f} "
                f">= {self.max_daily_loss:.2%}",
            )

        if account_status.weekly_loss_percent >= self.max_weekly_loss:
            return self._reject(
                "RISK_WEEKLY_STOP",
                f"weekly_loss_percent={account_status.weekly_loss_percent:.4f} "
                f">= {self.max_weekly_loss:.2%}",
            )

        if account_status.drawdown_percent >= self.max_drawdown:
            return self._reject(
                "RISK_DRAWDOWN_STOP",
                f"drawdown_percent={account_status.drawdown_percent:.4f} "
                f">= {self.max_drawdown:.2%}",
            )

        # --- Graduated loss streak throttle ---
        losses = account_status.consecutive_losses
        if losses >= self.LOSS_HALT_THRESHOLD:
            return self._reject(
                "RISK_LOSS_STREAK",
                f"consecutive_losses={losses} >= halt_threshold={self.LOSS_HALT_THRESHOLD}",
            )
        throttle = self.LOSS_THROTTLE.get(losses, 1.0)
        effective_risk = proposed_risk_percent * throttle

        # --- Max simultaneous trades gate ---
        if account_status.open_positions_count >= self.max_simultaneous_trades:
            return self._reject(
                "RISK_MAX_SIMULTANEOUS",
                f"open_positions_count={account_status.open_positions_count} "
                f">= max={self.max_simultaneous_trades}",
            )

        # --- Combined exposure gate ---
        ceiling = self._combined_exposure_limit(account_status.balance)
        if account_status.open_risk_percent + effective_risk > ceiling:
            mode = (
                f"fixed_usd=${self._fixed_risk_usd:.2f} ceiling={ceiling:.6f}"
                if self._fixed_risk_usd is not None
                else f"pct ceiling={ceiling:.4f}"
            )
            return self._reject(
                "RISK_COMBINED_EXPOSURE",
                (
                    f"open_risk_percent={account_status.open_risk_percent:.6f}, "
                    f"effective={effective_risk:.6f}, {mode}"
                ),
            )

        return RiskDecision(
            approved=True,
            reason_code="RISK_APPROVED",
            details=(
                f"Hard risk constraints satisfied "
                f"[throttle={throttle:.2f} effective_risk={effective_risk:.6f}]"
            ),
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
