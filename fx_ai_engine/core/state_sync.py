from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.account_status import AccountStatus
from core.risk.exposure_manager import is_usd_pair


def _derive_usd_exposure_count(open_symbols: list[str]) -> int:
    return sum(1 for sym in open_symbols if is_usd_pair(sym))


def _period_loss_percent(anchor_equity: float, equity: float) -> float:
    if anchor_equity <= 0:
        return 0.0
    return max(0.0, (anchor_equity - equity) / anchor_equity)


def update_account_status_from_snapshot(
    account_status: AccountStatus,
    snapshot: dict[str, Any],
) -> AccountStatus:
    """Maps MT5 snapshot payload into runtime AccountStatus in-place."""
    if not snapshot or snapshot.get("error"):
        account_status.is_trading_halted = True
        return account_status

    account_status.is_trading_halted = False

    balance = float(snapshot.get("balance", account_status.balance))
    equity = float(snapshot.get("equity", account_status.equity))

    account_status.balance = balance
    account_status.equity = equity
    account_status.margin_free = float(snapshot.get("margin_free", 0.0))
    account_status.open_positions_count = int(snapshot.get("open_positions_count", 0))
    account_status.open_symbols = list(snapshot.get("open_symbols", []))
    account_status.floating_pnl = float(snapshot.get("floating_pnl", 0.0))
    account_status.open_risk_percent = float(snapshot.get("open_risk_percent", account_status.open_risk_percent))
    account_status.open_usd_exposure_count = int(
        snapshot.get(
            "open_usd_exposure_count",
            _derive_usd_exposure_count(account_status.open_symbols),
        )
    )

    if account_status.peak_equity <= 0:
        account_status.peak_equity = equity
    else:
        account_status.peak_equity = max(account_status.peak_equity, equity)

    if account_status.peak_equity > 0:
        account_status.drawdown_percent = (
            (account_status.peak_equity - equity) / account_status.peak_equity
        )

    now_utc = datetime.now(timezone.utc)
    day_key = now_utc.strftime("%Y-%m-%d")
    week_key = now_utc.strftime("%G-W%V")

    # Reset per-period anchors when a new day/week begins.
    if account_status.daily_anchor_date != day_key:
        account_status.daily_anchor_date = day_key
        account_status.daily_anchor_equity = equity
    if account_status.weekly_anchor_key != week_key:
        account_status.weekly_anchor_key = week_key
        account_status.weekly_anchor_equity = equity

    # Prefer explicit snapshot fields when EA provides them.
    if "daily_loss_percent" in snapshot:
        account_status.daily_loss_percent = float(snapshot.get("daily_loss_percent", 0.0))
    else:
        account_status.daily_loss_percent = _period_loss_percent(account_status.daily_anchor_equity, equity)

    if "weekly_loss_percent" in snapshot:
        account_status.weekly_loss_percent = float(snapshot.get("weekly_loss_percent", 0.0))
    else:
        account_status.weekly_loss_percent = _period_loss_percent(account_status.weekly_anchor_equity, equity)

    if "consecutive_losses" in snapshot:
        account_status.consecutive_losses = int(snapshot.get("consecutive_losses", 0))

    account_status.updated_at = datetime.now(timezone.utc)
    return account_status
