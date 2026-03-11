from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.account_status import AccountStatus
from core.risk.exposure_manager import is_usd_pair

_RECONCILIATION_TOLERANCE = 1e-6


def _derive_usd_exposure_count(open_symbols: list[str]) -> int:
    return sum(1 for sym in open_symbols if is_usd_pair(sym))


def _period_loss_percent(anchor_equity: float, equity: float) -> float:
    if anchor_equity <= 0:
        return 0.0
    return max(0.0, (anchor_equity - equity) / anchor_equity)


def _format_float_token(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.6f}"


def _format_ticket_list(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    tickets: list[int] = []
    for item in raw:
        try:
            tickets.append(int(item))
        except (TypeError, ValueError):
            continue
    return tickets


def _ledger_detail_suffix(
    *,
    broker_symbols: list[str],
    broker_open_risk: float | None,
    trade_ledger: dict[str, Any] | None,
) -> str:
    ledger = trade_ledger or {}
    return (
        f" broker_symbols={sorted(set(broker_symbols))}"
        f" local_symbols={sorted(set(str(symbol) for symbol in ledger.get('open_symbols', []) if symbol))}"
        f" broker_open_risk={_format_float_token(broker_open_risk)}"
        f" local_open_risk={_format_float_token(float(ledger.get('open_risk_percent', 0.0) or 0.0))}"
        f" local_trade_ids={list(ledger.get('open_trade_ids', []) or [])}"
        f" local_trade_tickets={list(ledger.get('open_trade_tickets', []) or [])}"
        f" local_position_tickets={list(ledger.get('open_position_tickets', []) or [])}"
        f" local_statuses={list(ledger.get('open_statuses', []) or [])}"
    )


def _mark_unreconciled(account_status: AccountStatus, reason: str) -> None:
    account_status.state_reconciled = False
    account_status.is_trading_halted = True
    if not account_status.state_reconciliation_reason:
        account_status.state_reconciliation_reason = reason
    elif reason not in account_status.state_reconciliation_reason:
        account_status.state_reconciliation_reason = (
            f"{account_status.state_reconciliation_reason}; {reason}"
        )


def _seed_restart_state(
    account_status: AccountStatus,
    persisted_state: dict[str, Any] | None,
) -> None:
    if not persisted_state:
        return

    account_status.peak_equity = max(
        account_status.peak_equity,
        float(persisted_state.get("peak_equity", 0.0) or 0.0),
    )
    if not account_status.daily_anchor_date:
        account_status.daily_anchor_date = str(persisted_state.get("daily_anchor_date", "") or "")
    if account_status.daily_anchor_equity <= 0:
        account_status.daily_anchor_equity = float(
            persisted_state.get("daily_anchor_equity", 0.0) or 0.0
        )
    if not account_status.weekly_anchor_key:
        account_status.weekly_anchor_key = str(persisted_state.get("weekly_anchor_key", "") or "")
    if account_status.weekly_anchor_equity <= 0:
        account_status.weekly_anchor_equity = float(
            persisted_state.get("weekly_anchor_equity", 0.0) or 0.0
        )
    account_status.open_risk_percent = max(
        account_status.open_risk_percent,
        float(persisted_state.get("open_risk_percent", 0.0) or 0.0),
    )


def update_account_status_from_snapshot(
    account_status: AccountStatus,
    snapshot: dict[str, Any],
    *,
    persisted_state: dict[str, Any] | None = None,
    trade_ledger: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
) -> AccountStatus:
    """Maps MT5 snapshot payload into runtime AccountStatus in-place."""
    _seed_restart_state(account_status, persisted_state)
    account_status.state_reconciled = True
    account_status.state_reconciliation_reason = ""

    if not snapshot or snapshot.get("error"):
        account_status.is_trading_halted = True
        error_code = (snapshot or {}).get("error", {}).get("code", "BROKER_SNAPSHOT_UNAVAILABLE")
        _mark_unreconciled(account_status, f"broker snapshot unavailable: {error_code}")
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
    account_status.open_usd_exposure_count = int(
        snapshot.get(
            "open_usd_exposure_count",
            _derive_usd_exposure_count(account_status.open_symbols),
        )
    )

    persisted_open_risk = float((persisted_state or {}).get("open_risk_percent", 0.0) or 0.0)
    ledger_available = trade_ledger is not None
    ledger_open_risk = float((trade_ledger or {}).get("open_risk_percent", 0.0) or 0.0)
    ledger_count = int((trade_ledger or {}).get("open_trade_count", 0) or 0)
    ledger_symbols = list((trade_ledger or {}).get("open_symbols", []) or [])
    fallback_open_risk = max(account_status.open_risk_percent, persisted_open_risk, ledger_open_risk)
    broker_open_risk = (
        float(snapshot.get("open_risk_percent", 0.0))
        if "open_risk_percent" in snapshot
        else None
    )
    detail_suffix = _ledger_detail_suffix(
        broker_symbols=account_status.open_symbols,
        broker_open_risk=broker_open_risk,
        trade_ledger=trade_ledger,
    )
    management_state_restored = snapshot.get("management_state_restored")
    managed_positions_count = int(snapshot.get("managed_positions_count", 0) or 0)
    managed_position_tickets = _format_ticket_list(snapshot.get("managed_position_tickets", []))
    unmanaged_position_tickets = _format_ticket_list(snapshot.get("unmanaged_position_tickets", []))
    management_state_error = str(snapshot.get("management_state_error", "") or "")

    if account_status.open_positions_count <= 0:
        account_status.open_risk_percent = fallback_open_risk if ledger_available and ledger_count > 0 else 0.0
        if ledger_available and ledger_count > 0:
            _mark_unreconciled(
                account_status,
                f"broker_positions=0 but local_open_trades={ledger_count}{detail_suffix}",
            )
    else:
        account_status.open_risk_percent = broker_open_risk if broker_open_risk is not None else fallback_open_risk
        if management_state_restored is False or managed_positions_count < account_status.open_positions_count:
            error_suffix = f" error={management_state_error}" if management_state_error else ""
            _mark_unreconciled(
                account_status,
                "broker management-state restore failed "
                f"managed_positions={managed_positions_count}/{account_status.open_positions_count} "
                f"managed_tickets={managed_position_tickets} unmanaged_tickets={unmanaged_position_tickets}{error_suffix}{detail_suffix}",
            )
        if ledger_available and ledger_count != account_status.open_positions_count:
            _mark_unreconciled(
                account_status,
                "broker/local open-position mismatch "
                f"broker_positions={account_status.open_positions_count} local_open_trades={ledger_count}{detail_suffix}",
            )
        if (
            ledger_available
            and account_status.open_symbols
            and ledger_symbols
            and set(account_status.open_symbols) != set(ledger_symbols)
        ):
            _mark_unreconciled(
                account_status,
                "broker/local open-symbol mismatch "
                f"broker={sorted(set(account_status.open_symbols))} local={sorted(set(ledger_symbols))}{detail_suffix}",
            )
        if (
            ledger_available
            and broker_open_risk is not None
            and ledger_count == account_status.open_positions_count
            and ledger_count > 0
            and abs(broker_open_risk - ledger_open_risk) > _RECONCILIATION_TOLERANCE
        ):
            account_status.open_risk_percent = max(broker_open_risk, fallback_open_risk)
            _mark_unreconciled(
                account_status,
                "broker/local open-risk mismatch "
                f"broker_open_risk={broker_open_risk:.6f} local_open_risk={ledger_open_risk:.6f}{detail_suffix}",
            )

    account_status.peak_equity = max(
        account_status.peak_equity,
        float((persisted_state or {}).get("peak_equity", 0.0) or 0.0),
        equity,
    )

    if account_status.peak_equity > 0:
        account_status.drawdown_percent = (
            (account_status.peak_equity - equity) / account_status.peak_equity
        )

    now_utc = now_utc or datetime.now(timezone.utc)
    day_key = now_utc.strftime("%Y-%m-%d")
    week_key = now_utc.strftime("%G-W%V")

    persisted_daily_anchor_date = account_status.daily_anchor_date or str(
        (persisted_state or {}).get("daily_anchor_date", "") or ""
    )
    persisted_daily_anchor_equity = account_status.daily_anchor_equity or float(
        (persisted_state or {}).get("daily_anchor_equity", 0.0) or 0.0
    )
    if persisted_daily_anchor_date == day_key and persisted_daily_anchor_equity > 0:
        account_status.daily_anchor_date = persisted_daily_anchor_date
        account_status.daily_anchor_equity = persisted_daily_anchor_equity
    else:
        account_status.daily_anchor_date = day_key
        account_status.daily_anchor_equity = equity

    persisted_weekly_anchor_key = account_status.weekly_anchor_key or str(
        (persisted_state or {}).get("weekly_anchor_key", "") or ""
    )
    persisted_weekly_anchor_equity = account_status.weekly_anchor_equity or float(
        (persisted_state or {}).get("weekly_anchor_equity", 0.0) or 0.0
    )
    if persisted_weekly_anchor_key == week_key and persisted_weekly_anchor_equity > 0:
        account_status.weekly_anchor_key = persisted_weekly_anchor_key
        account_status.weekly_anchor_equity = persisted_weekly_anchor_equity
    else:
        account_status.weekly_anchor_key = week_key
        account_status.weekly_anchor_equity = equity

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
    else:
        account_status.consecutive_losses = int(
            (persisted_state or {}).get("consecutive_losses", account_status.consecutive_losses) or 0
        )

    account_status.updated_at = now_utc
    return account_status
