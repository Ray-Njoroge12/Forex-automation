from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class AccountStatus:
    balance: float = 0.0
    equity: float = 0.0
    daily_loss_percent: float = 0.0
    weekly_loss_percent: float = 0.0
    drawdown_percent: float = 0.0
    open_risk_percent: float = 0.0
    open_usd_exposure_count: int = 0
    consecutive_losses: int = 0
    is_trading_halted: bool = False
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    open_positions_count: int = 0
    open_symbols: list[str] = field(default_factory=list)
    floating_pnl: float = 0.0
    margin_free: float = 0.0
    peak_equity: float = 0.0

    def is_stale(self, now_utc: datetime | None = None, max_age_seconds: int = 120) -> bool:
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        return now_utc - self.updated_at > timedelta(seconds=max_age_seconds)
