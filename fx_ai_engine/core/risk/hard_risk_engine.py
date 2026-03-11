from __future__ import annotations

from datetime import datetime, timezone

from config_microcapital import get_policy_config, read_fixed_risk_usd
from core.account_status import AccountStatus
from core.types import RiskDecision


def _read_fixed_risk_usd() -> float | None:
    """Return runtime fixed-risk USD from env override or active policy."""
    return read_fixed_risk_usd()


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

    Policy Modes
    ------------
    Core SRS is the locked baseline. Preserve-$10 is an explicit policy mode.
    The historical MICRO_CAPITAL_MODE toggle remains a backward-compatible
    alias for the legacy micro-capital path only.

    Loss-Streak Throttle
    --------------------
    Core SRS:
      Streak 1 → 75% of base risk
      Streak 2 → 50%
      Streak >= 3 → full halt

    Preserve-$10 / legacy micro-capital:
      Streak 1 → 75% of base risk
      Streak >= 2 → full halt
    """

    # Graduated loss streak: reduce risk multiplier before halting entirely.
    LOSS_THROTTLE: dict[int, float] = {1: 0.75, 2: 0.50}
    LOSS_HALT_THRESHOLD = 3

    def __init__(self, policy: dict | None = None):
        self.policy = get_policy_config() if policy is None else dict(policy)
        self.mode_id = self.policy["MODE_ID"]
        self.mode_label = self.policy["MODE_LABEL"]
        self.evidence_label = self.policy["EVIDENCE_LABEL"]
        self.max_daily_loss = self.policy["DAILY_STOP_LOSS_PCT"]
        self.max_weekly_loss = self.policy["WEEKLY_STOP_LOSS_PCT"]
        self.max_drawdown = self.policy["HARD_DRAWDOWN_PCT"]
        self.max_simultaneous_trades = self.policy["MAX_SIMULTANEOUS_TRADES"]
        self.max_combined_exposure = self.policy["MAX_COMBINED_EXPOSURE"]
        self.LOSS_HALT_THRESHOLD = self.policy["LOSS_HALT_THRESHOLD"]
        self.loss_throttle = dict(self.policy.get("LOSS_THROTTLE_STEPS", self.LOSS_THROTTLE))

        # Fixed-USD mode: ceiling is 2x per-trade risk as a fraction (set dynamically).
        self._fixed_risk_usd = self.policy.get("FIXED_RISK_USD") if policy is not None else _read_fixed_risk_usd()

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

        if not account_status.state_reconciled:
            return self._reject(
                "RISK_STATE_DIVERGENCE",
                (
                    "Trading blocked because broker/local risk state is not reconciled; the engine is fail-closed until the mismatch is cleared. "
                    f"Observed state issue: {account_status.state_reconciliation_reason or 'risk state not reconciled'}"
                ),
            )

        if account_status.is_trading_halted:
            return self._reject(
                "RISK_HALTED",
                "Trading remains halted in runtime account state; no new trades will be routed until the halt condition is cleared",
            )

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
        throttle = self.loss_throttle.get(losses, 1.0)
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
                f"[mode={self.mode_id} evidence={self.evidence_label} throttle={throttle:.2f} "
                f"effective_risk={effective_risk:.6f}]"
            ),
            timestamp_utc=now,
            risk_throttle_multiplier=throttle,
        )

    def _reject(self, code: str, details: str) -> RiskDecision:
        return RiskDecision(
            approved=False,
            reason_code=code,
            details=f"{details} [mode={self.mode_id} evidence={self.evidence_label}]",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )
