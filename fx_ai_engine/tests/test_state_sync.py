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


def test_state_sync_restores_restart_critical_fields_from_persisted_state() -> None:
    status = AccountStatus()
    now_utc = datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)
    snapshot = {
        "timestamp": now_utc.isoformat(),
        "balance": 980.0,
        "equity": 970.0,
        "margin_free": 700.0,
        "open_positions_count": 0,
        "floating_pnl": -10.0,
    }
    persisted_state = {
        "peak_equity": 1200.0,
        "daily_anchor_date": "2026-03-06",
        "daily_anchor_equity": 1000.0,
        "weekly_anchor_key": "2026-W10",
        "weekly_anchor_equity": 1050.0,
        "consecutive_losses": 2,
        "open_risk_percent": 0.012,
    }

    update_account_status_from_snapshot(
        status,
        snapshot,
        persisted_state=persisted_state,
        now_utc=now_utc,
    )

    assert status.peak_equity == 1200.0
    assert status.daily_anchor_date == "2026-03-06"
    assert status.daily_anchor_equity == 1000.0
    assert status.weekly_anchor_key == "2026-W10"
    assert status.weekly_anchor_equity == 1050.0
    assert status.consecutive_losses == 2
    assert round(status.daily_loss_percent, 6) == 0.03
    assert round(status.weekly_loss_percent, 6) == round((1050.0 - 970.0) / 1050.0, 6)
    assert status.state_reconciled is True


def test_state_sync_halts_on_broker_trade_ledger_mismatch() -> None:
    status = AccountStatus()
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "balance": 1000.0,
        "equity": 995.0,
        "margin_free": 800.0,
        "open_positions_count": 1,
        "open_symbols": ["EURUSD"],
        "floating_pnl": -5.0,
    }

    update_account_status_from_snapshot(
        status,
        snapshot,
        persisted_state={"open_risk_percent": 0.032},
        trade_ledger={"open_trade_count": 0, "open_risk_percent": 0.0, "open_symbols": []},
    )

    assert status.is_trading_halted is True
    assert status.state_reconciled is False
    assert "broker_positions=1 local_open_trades=0" in status.state_reconciliation_reason
    assert status.open_risk_percent == 0.032


def test_state_sync_halts_on_open_risk_disagreement() -> None:
    status = AccountStatus()
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "balance": 1000.0,
        "equity": 990.0,
        "margin_free": 800.0,
        "open_positions_count": 1,
        "open_symbols": ["EURUSD"],
        "floating_pnl": -10.0,
        "open_risk_percent": 0.04,
    }

    update_account_status_from_snapshot(
        status,
        snapshot,
        trade_ledger={"open_trade_count": 1, "open_risk_percent": 0.02, "open_symbols": ["EURUSD"]},
    )

    assert status.is_trading_halted is True
    assert status.state_reconciled is False
    assert "broker/local open-risk mismatch" in status.state_reconciliation_reason
    assert status.open_risk_percent == 0.04


def test_state_sync_halts_when_broker_is_flat_but_local_trade_remains() -> None:
    status = AccountStatus()
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "balance": 1000.0,
        "equity": 995.0,
        "margin_free": 800.0,
        "open_positions_count": 0,
        "open_symbols": [],
        "floating_pnl": -5.0,
    }

    update_account_status_from_snapshot(
        status,
        snapshot,
        persisted_state={"open_risk_percent": 0.032},
        trade_ledger={"open_trade_count": 1, "open_risk_percent": 0.032, "open_symbols": ["EURUSD"]},
    )

    assert status.is_trading_halted is True
    assert status.state_reconciled is False
    assert "broker_positions=0 but local_open_trades=1" in status.state_reconciliation_reason
    assert status.open_risk_percent == 0.032
