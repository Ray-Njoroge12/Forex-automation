from __future__ import annotations

import math
from dataclasses import dataclass

from config_microcapital import get_policy_config

SMOKE_TEST_STARTING_CASH = 10_000.0
REALISTIC_FX_CONTRACT_SIZE_UNITS = 100_000.0
REALISTIC_MIN_LOT = 0.01
REALISTIC_LOT_STEP = 0.01
REALISTIC_MAX_LOT = 100.0
REALISTIC_SPREAD_PIPS = 1.8
REALISTIC_SLIPPAGE_PIPS = 0.3
REALISTIC_COMMISSION_PER_LOT_USD = 7.0


@dataclass(frozen=True)
class SimulationProfile:
    mode_id: str
    mode_label: str
    evidence_label: str
    evidence_stream: str
    realism_label: str
    starting_cash: float
    fixed_risk_usd: float | None
    base_spread_pips: float
    slippage_pips: float
    commission_per_lot_usd: float
    min_lot: float
    lot_step: float
    max_lot: float
    contract_size_units: float
    realistic_constraints: bool


@dataclass(frozen=True)
class SimulatedTradeDecision:
    approved: bool
    reason_code: str
    details: str
    estimated_lot: float = 0.0
    estimated_units: float = 0.0
    round_trip_cost_usd: float = 0.0
    effective_risk_amount_usd: float = 0.0


def build_simulation_profile(mode_id: str | None = None) -> SimulationProfile:
    policy = get_policy_config(mode_id=mode_id)
    realistic = policy["MODE_ID"] == "preserve_10"
    if not realistic:
        return SimulationProfile(
            mode_id=policy["MODE_ID"],
            mode_label=policy["MODE_LABEL"],
            evidence_label=policy["EVIDENCE_LABEL"],
            evidence_stream="core_srs_smoke_test",
            realism_label="Smoke-test simulation",
            starting_cash=SMOKE_TEST_STARTING_CASH,
            fixed_risk_usd=None,
            base_spread_pips=1.2,
            slippage_pips=0.0,
            commission_per_lot_usd=0.0,
            min_lot=0.0,
            lot_step=0.0,
            max_lot=0.0,
            contract_size_units=REALISTIC_FX_CONTRACT_SIZE_UNITS,
            realistic_constraints=False,
        )

    return SimulationProfile(
        mode_id=policy["MODE_ID"],
        mode_label=policy["MODE_LABEL"],
        evidence_label=policy["EVIDENCE_LABEL"],
        evidence_stream="preserve_10_realistic",
        realism_label="Preserve-$10 realistic simulation",
        starting_cash=10.0,
        fixed_risk_usd=policy["FIXED_RISK_USD"],
        base_spread_pips=REALISTIC_SPREAD_PIPS,
        slippage_pips=REALISTIC_SLIPPAGE_PIPS,
        commission_per_lot_usd=REALISTIC_COMMISSION_PER_LOT_USD,
        min_lot=REALISTIC_MIN_LOT,
        lot_step=REALISTIC_LOT_STEP,
        max_lot=REALISTIC_MAX_LOT,
        contract_size_units=REALISTIC_FX_CONTRACT_SIZE_UNITS,
        realistic_constraints=True,
    )


def convert_quote_pnl_to_usd(symbol: str, quote_pnl: float, reference_price: float) -> float:
    if symbol.endswith("USD"):
        return quote_pnl
    if symbol.startswith("USD") and reference_price > 0:
        return quote_pnl / reference_price
    return quote_pnl


def assess_trade_feasibility(
    *,
    symbol: str,
    entry_price: float,
    stop_pips: float,
    risk_amount_usd: float,
    profile: SimulationProfile,
) -> SimulatedTradeDecision:
    if not profile.realistic_constraints:
        return SimulatedTradeDecision(
            approved=True,
            reason_code="SMOKE_TEST_BYPASS",
            details=f"simulation={profile.evidence_stream} bypasses realistic execution constraints",
        )

    if entry_price <= 0 or stop_pips <= 0 or risk_amount_usd <= 0:
        return SimulatedTradeDecision(
            approved=False,
            reason_code="SIMULATION_INPUT_INVALID",
            details=(
                f"symbol={symbol} entry_price={entry_price:.5f} stop_pips={stop_pips:.2f} "
                f"risk_amount_usd={risk_amount_usd:.4f}"
            ),
        )

    per_lot_stop_usd = _per_lot_price_risk_usd(symbol, entry_price, stop_pips, profile)
    per_lot_cost_usd = _per_lot_round_trip_cost_usd(symbol, entry_price, profile)
    per_lot_total_usd = per_lot_stop_usd + per_lot_cost_usd
    raw_lot = risk_amount_usd / per_lot_total_usd if per_lot_total_usd > 0 else 0.0

    if raw_lot < profile.min_lot:
        return SimulatedTradeDecision(
            approved=False,
            reason_code="REJECTED_LOT_PREROUTE",
            details=(
                f"symbol={symbol} entry={entry_price:.5f} risk_amount={risk_amount_usd:.4f} "
                f"stop_pips={stop_pips:.2f} raw_lot={raw_lot:.6f} min_lot={profile.min_lot:.4f} "
                f"cost_per_lot_usd={per_lot_cost_usd:.4f}"
            ),
        )

    quantized_lot = math.floor(raw_lot / profile.lot_step) * profile.lot_step
    if profile.max_lot > 0:
        quantized_lot = min(quantized_lot, profile.max_lot)
    quantized_lot = round(max(quantized_lot, 0.0), 8)

    if quantized_lot < profile.min_lot:
        return SimulatedTradeDecision(
            approved=False,
            reason_code="REJECTED_LOT_PREROUTE",
            details=(
                f"symbol={symbol} entry={entry_price:.5f} risk_amount={risk_amount_usd:.4f} "
                f"stop_pips={stop_pips:.2f} quantized_lot={quantized_lot:.6f} min_lot={profile.min_lot:.4f}"
            ),
        )

    round_trip_cost_usd = round(per_lot_cost_usd * quantized_lot, 8)
    stop_risk_usd = round(per_lot_stop_usd * quantized_lot, 8)
    return SimulatedTradeDecision(
        approved=True,
        reason_code="TRADE_FEASIBLE",
        details=(
            f"symbol={symbol} entry={entry_price:.5f} stop_pips={stop_pips:.2f} raw_lot={raw_lot:.6f} "
            f"estimated_lot={quantized_lot:.6f} round_trip_cost_usd={round_trip_cost_usd:.4f}"
        ),
        estimated_lot=quantized_lot,
        estimated_units=round(quantized_lot * profile.contract_size_units, 4),
        round_trip_cost_usd=round_trip_cost_usd,
        effective_risk_amount_usd=round(stop_risk_usd + round_trip_cost_usd, 8),
    )


def _pip_size(symbol: str) -> float:
    return 0.01 if "JPY" in symbol else 0.0001


def _per_lot_price_risk_usd(
    symbol: str,
    entry_price: float,
    pips: float,
    profile: SimulationProfile,
) -> float:
    quote_risk = _pip_size(symbol) * pips * profile.contract_size_units
    return convert_quote_pnl_to_usd(symbol, quote_risk, entry_price)


def _per_lot_round_trip_cost_usd(
    symbol: str,
    entry_price: float,
    profile: SimulationProfile,
) -> float:
    friction_pips = profile.base_spread_pips + profile.slippage_pips
    market_friction_usd = _per_lot_price_risk_usd(symbol, entry_price, friction_pips, profile)
    return market_friction_usd + profile.commission_per_lot_usd