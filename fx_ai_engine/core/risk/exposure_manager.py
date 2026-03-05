from __future__ import annotations

from core.account_status import AccountStatus

USD_PAIRS = {"EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCHF", "USDCAD"}


def is_usd_pair(symbol: str) -> bool:
    return symbol in USD_PAIRS


def can_add_usd_exposure(account_status: AccountStatus, max_usd_exposure_count: int = 2) -> bool:
    return account_status.open_usd_exposure_count < max_usd_exposure_count


def estimate_combined_exposure(account_status: AccountStatus, new_risk_percent: float) -> float:
    return account_status.open_risk_percent + new_risk_percent
