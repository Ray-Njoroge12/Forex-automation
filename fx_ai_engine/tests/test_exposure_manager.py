from __future__ import annotations

from core.account_status import AccountStatus
from core.risk.exposure_manager import (
    can_add_usd_exposure,
    estimate_combined_exposure,
    is_usd_pair,
)


def test_exposure_helpers() -> None:
    status = AccountStatus(open_risk_percent=0.03, open_usd_exposure_count=1)
    assert is_usd_pair("EURUSD") is True
    assert is_usd_pair("EURGBP") is False
    assert can_add_usd_exposure(status, max_usd_exposure_count=2) is True
    assert estimate_combined_exposure(status, 0.02) == 0.05
