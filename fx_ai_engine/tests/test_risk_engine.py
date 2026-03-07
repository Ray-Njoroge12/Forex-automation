from __future__ import annotations

from datetime import datetime, timezone

from core.account_status import AccountStatus
from core.risk.hard_risk_engine import HardRiskEngine
from core.risk.reset_scheduler import ResetScheduler


def _status(**kwargs) -> AccountStatus:
    base = AccountStatus(
        balance=1000.0,
        equity=1000.0,
        daily_loss_percent=0.0,
        weekly_loss_percent=0.0,
        drawdown_percent=0.0,
        open_risk_percent=0.0,
        open_usd_exposure_count=0,
        consecutive_losses=0,
        is_trading_halted=False,
        open_positions_count=0,
    )
    for key, value in kwargs.items():
        setattr(base, key, value)
    return base


def test_hard_risk_blocks_daily_stop() -> None:
    engine = HardRiskEngine()
    result = engine.validate(_status(daily_loss_percent=0.08), proposed_risk_percent=0.01)
    assert result.approved is False
    assert result.reason_code == "RISK_DAILY_STOP"


def test_hard_risk_blocks_unreconciled_state() -> None:
    engine = HardRiskEngine()
    result = engine.validate(
        _status(state_reconciled=False, state_reconciliation_reason="broker/local mismatch"),
        proposed_risk_percent=0.01,
    )
    assert result.approved is False
    assert result.reason_code == "RISK_STATE_DIVERGENCE"
    assert "Trading blocked because broker/local risk state is not reconciled" in result.details
    assert "broker/local mismatch" in result.details
    assert "evidence=Core SRS v1" in result.details


def test_hard_risk_blocks_explicit_halt_after_reconciliation() -> None:
    engine = HardRiskEngine()
    result = engine.validate(
        _status(is_trading_halted=True, state_reconciled=True),
        proposed_risk_percent=0.01,
    )
    assert result.approved is False
    assert result.reason_code == "RISK_HALTED"
    assert "Trading remains halted in runtime account state" in result.details
    assert "no new trades will be routed" in result.details


def test_hard_risk_blocks_weekly_stop() -> None:
    engine = HardRiskEngine()
    result = engine.validate(_status(weekly_loss_percent=0.15), proposed_risk_percent=0.01)
    assert result.approved is False
    assert result.reason_code == "RISK_WEEKLY_STOP"


def test_hard_risk_blocks_drawdown_and_streak() -> None:
    engine = HardRiskEngine()
    drawdown = engine.validate(_status(drawdown_percent=0.2), proposed_risk_percent=0.01)
    streak = engine.validate(_status(consecutive_losses=3), proposed_risk_percent=0.01)
    assert drawdown.reason_code == "RISK_DRAWDOWN_STOP"
    assert streak.reason_code == "RISK_LOSS_STREAK"


def test_hard_risk_graduated_throttle() -> None:
    """Loss streaks 1-2 reduce risk via multiplier; 3 halts per locked SRS."""
    engine = HardRiskEngine()

    for losses, expected_throttle in [(1, 0.75), (2, 0.50)]:
        result = engine.validate(_status(consecutive_losses=losses), proposed_risk_percent=0.01)
        assert result.approved is True, f"Expected approval at {losses} consecutive losses"
        assert result.risk_throttle_multiplier == expected_throttle, (
            f"Expected throttle {expected_throttle} at {losses} losses, "
            f"got {result.risk_throttle_multiplier}"
        )

    halted = engine.validate(_status(consecutive_losses=3), proposed_risk_percent=0.01)
    assert halted.approved is False
    assert halted.reason_code == "RISK_LOSS_STREAK"

    # No losses → no throttle.
    clean = engine.validate(_status(consecutive_losses=0), proposed_risk_percent=0.01)
    assert clean.approved is True
    assert clean.risk_throttle_multiplier == 1.0


def test_preserve_10_halt_policy_is_explicit(monkeypatch) -> None:
    monkeypatch.setenv("FX_POLICY_MODE", "preserve_10")
    monkeypatch.delenv("MICRO_CAPITAL_MODE", raising=False)

    engine = HardRiskEngine()

    throttled = engine.validate(_status(consecutive_losses=1), proposed_risk_percent=0.0)
    assert throttled.approved is True
    assert throttled.risk_throttle_multiplier == 0.75

    halted = engine.validate(_status(consecutive_losses=2), proposed_risk_percent=0.0)
    assert halted.approved is False
    assert halted.reason_code == "RISK_LOSS_STREAK"


def test_hard_risk_blocks_combined_exposure() -> None:
    engine = HardRiskEngine()
    result = engine.validate(_status(open_risk_percent=0.04), proposed_risk_percent=0.02)
    assert result.approved is False
    assert result.reason_code == "RISK_COMBINED_EXPOSURE"


def test_reset_scheduler_daily_and_weekly_boundaries() -> None:
    scheduler = ResetScheduler()

    monday_utc = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
    tuesday_utc = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)

    assert scheduler.should_reset_daily(monday_utc) is True
    assert scheduler.should_reset_daily(monday_utc) is False
    assert scheduler.should_reset_daily(tuesday_utc) is True

    assert scheduler.should_reset_weekly(monday_utc) is True
    assert scheduler.should_reset_weekly(monday_utc) is False
    assert scheduler.should_reset_weekly(tuesday_utc) is False
