from __future__ import annotations

from datetime import datetime, timezone

from core.account_status import AccountStatus
from core.state_sync import update_account_status_from_snapshot


def test_state_sync_updates_numeric_fields_and_drawdown() -> None:
    status = AccountStatus(balance=1000.0, equity=1000.0)
    status.peak_equity = 1200.0

    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "balance": 980.0,
        "equity": 900.0,
        "margin_free": 700.0,
        "open_positions_count": 1,
        "floating_pnl": -80.0,
        "open_symbols": ["EURUSD", "USDJPY"],
    }

    update_account_status_from_snapshot(status, snapshot)

    assert status.balance == 980.0
    assert status.equity == 900.0
    assert status.margin_free == 700.0
    assert status.open_positions_count == 1
    assert status.floating_pnl == -80.0
    assert status.open_symbols == ["EURUSD", "USDJPY"]
    assert status.open_usd_exposure_count == 2
    assert round(status.drawdown_percent, 6) == round((1200.0 - 900.0) / 1200.0, 6)
    assert status.daily_loss_percent >= 0.0
    assert status.weekly_loss_percent >= 0.0


def test_state_sync_halts_on_error_payload() -> None:
    status = AccountStatus(is_trading_halted=False)

    update_account_status_from_snapshot(
        status,
        {
            "error": {
                "code": "MT5_NOT_CONNECTED",
                "message": "test",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            }
        },
    )

    assert status.is_trading_halted is True


def test_state_sync_prefers_snapshot_loss_fields_when_present() -> None:
    status = AccountStatus(balance=1000.0, equity=1000.0)
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "balance": 980.0,
        "equity": 970.0,
        "margin_free": 700.0,
        "open_positions_count": 1,
        "floating_pnl": -10.0,
        "daily_loss_percent": 0.04,
        "weekly_loss_percent": 0.09,
        "open_usd_exposure_count": 1,
    }
    update_account_status_from_snapshot(status, snapshot)
    assert status.daily_loss_percent == 0.04
    assert status.weekly_loss_percent == 0.09
    assert status.open_usd_exposure_count == 1
