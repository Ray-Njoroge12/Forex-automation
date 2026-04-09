"""Comprehensive tests for ExposureManager — boundary conditions, edge cases, and
integration with the 5% MAX_COMBINED_EXPOSURE cap from Core SRS."""

from __future__ import annotations

from core.account_status import AccountStatus
from core.risk.exposure_manager import (
    can_add_usd_exposure,
    estimate_combined_exposure,
    is_usd_pair,
)


# ---------------------------------------------------------------------------
# is_usd_pair
# ---------------------------------------------------------------------------

class TestIsUsdPair:
    def test_all_supported_usd_pairs(self) -> None:
        for sym in ("EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCAD", "USDCHF"):
            assert is_usd_pair(sym) is True, f"{sym} should be a USD pair"

    def test_non_usd_pairs(self) -> None:
        for sym in ("EURGBP", "EURJPY", "GBPJPY", "AUDNZD", ""):
            assert is_usd_pair(sym) is False, f"{sym} should NOT be a USD pair"

    def test_case_sensitivity(self) -> None:
        assert is_usd_pair("eurusd") is False, "lookup is case-sensitive"


# ---------------------------------------------------------------------------
# can_add_usd_exposure
# ---------------------------------------------------------------------------

class TestCanAddUsdExposure:
    def test_below_limit(self) -> None:
        status = AccountStatus(open_usd_exposure_count=0)
        assert can_add_usd_exposure(status, max_usd_exposure_count=2) is True

    def test_at_limit(self) -> None:
        status = AccountStatus(open_usd_exposure_count=2)
        assert can_add_usd_exposure(status, max_usd_exposure_count=2) is False

    def test_above_limit(self) -> None:
        status = AccountStatus(open_usd_exposure_count=3)
        assert can_add_usd_exposure(status, max_usd_exposure_count=2) is False

    def test_custom_max_one(self) -> None:
        status = AccountStatus(open_usd_exposure_count=0)
        assert can_add_usd_exposure(status, max_usd_exposure_count=1) is True
        status2 = AccountStatus(open_usd_exposure_count=1)
        assert can_add_usd_exposure(status2, max_usd_exposure_count=1) is False

    def test_zero_max_always_blocked(self) -> None:
        status = AccountStatus(open_usd_exposure_count=0)
        assert can_add_usd_exposure(status, max_usd_exposure_count=0) is False


# ---------------------------------------------------------------------------
# estimate_combined_exposure
# ---------------------------------------------------------------------------

class TestEstimateCombinedExposure:
    def test_zero_existing_exposure(self) -> None:
        status = AccountStatus(open_risk_percent=0.0)
        assert estimate_combined_exposure(status, 0.032) == 0.032

    def test_zero_new_risk(self) -> None:
        status = AccountStatus(open_risk_percent=0.04)
        assert estimate_combined_exposure(status, 0.0) == 0.04

    def test_exceeds_five_percent_cap(self) -> None:
        status = AccountStatus(open_risk_percent=0.04)
        combined = estimate_combined_exposure(status, 0.032)
        assert combined > 0.05, "combined should exceed 5% cap"

    def test_at_exact_cap(self) -> None:
        status = AccountStatus(open_risk_percent=0.018)
        combined = estimate_combined_exposure(status, 0.032)
        assert combined == 0.05

    def test_small_fractional_risk(self) -> None:
        status = AccountStatus(open_risk_percent=0.001)
        combined = estimate_combined_exposure(status, 0.001)
        assert abs(combined - 0.002) < 1e-10


# ---------------------------------------------------------------------------
# Integration: MAX_COMBINED_EXPOSURE gate logic
# ---------------------------------------------------------------------------

class TestExposureGateIntegration:
    """Simulates the portfolio manager's exposure gate decision using the
    same logic as the real engine: block if combined > MAX_COMBINED_EXPOSURE."""

    MAX_COMBINED_EXPOSURE = 0.05

    def _would_be_blocked(self, open_risk: float, proposed_risk: float) -> bool:
        status = AccountStatus(open_risk_percent=open_risk)
        combined = estimate_combined_exposure(status, proposed_risk)
        return combined > self.MAX_COMBINED_EXPOSURE

    def test_first_trade_always_allowed(self) -> None:
        assert self._would_be_blocked(0.0, 0.032) is False

    def test_second_trade_allowed_if_within_cap(self) -> None:
        assert self._would_be_blocked(0.016, 0.032) is False

    def test_second_trade_blocked_if_over_cap(self) -> None:
        assert self._would_be_blocked(0.032, 0.032) is True

    def test_boundary_exactly_at_cap(self) -> None:
        assert self._would_be_blocked(0.018, 0.032) is False

    def test_tiny_overshoot_blocked(self) -> None:
        assert self._would_be_blocked(0.0181, 0.032) is True
