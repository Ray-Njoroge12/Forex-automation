from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.account_status import AccountStatus


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

    if account_status.peak_equity <= 0:
        account_status.peak_equity = equity
    else:
        account_status.peak_equity = max(account_status.peak_equity, equity)

    if account_status.peak_equity > 0:
        account_status.drawdown_percent = (
            (account_status.peak_equity - equity) / account_status.peak_equity
        )

    account_status.updated_at = datetime.now(timezone.utc)
    return account_status
