from __future__ import annotations

from datetime import datetime, timezone

from core.account_status import AccountStatus
from core.types import AdversarialDecision, PortfolioDecision, RegimeOutput, TechnicalSignal


class PortfolioManager:
    """Final pre-risk-engine allocator with exposure and stacking checks."""

    # Static correlation map (absolute values; pairs with |ρ| above threshold blocked).
    CORRELATED_PAIRS: dict[frozenset, float] = {
        frozenset(["EURUSD", "GBPUSD"]): 0.82,
        frozenset(["EURUSD", "AUDUSD"]): 0.71,
        frozenset(["USDCHF", "EURUSD"]): 0.89,
        frozenset(["USDCAD", "AUDUSD"]): 0.76,
    }
    CORRELATION_THRESHOLD = 0.70

    def __init__(self):
        self.max_simultaneous_trades = 2
        self.max_total_risk = 0.05
        self.base_risk = 0.032

    def _compute_dynamic_risk(self, volatility_state: str, atr_ratio: float) -> float:
        """Scale base risk by ATR ratio — widen in low vol, shrink in high vol.

        atr_ratio = current_atr / 90-bar_mean_atr:
          > 1.0  → more volatile than usual → reduce position size
          < 1.0  → quieter than usual → can push toward base risk
          = 1.0  → neutral
        Floor at 1.5% (0.015) so trades remain meaningful; cap at base_risk.
        """
        risk = self.base_risk / max(atr_ratio, 0.1)  # guard against zero
        return round(max(0.015, min(risk, self.base_risk)), 4)

    def evaluate(
        self,
        technical_signal: TechnicalSignal | None,
        adversarial: AdversarialDecision,
        account_status: AccountStatus,
        open_symbols: list[str] | None = None,
        regime: RegimeOutput | None = None,
    ) -> PortfolioDecision:
        now = datetime.now(timezone.utc).isoformat()

        if technical_signal is None:
            return PortfolioDecision(
                approved=False,
                final_risk_percent=0.0,
                reason_code="PM_NO_SIGNAL",
                details="No technical signal",
                timestamp_utc=now,
            )

        if not adversarial.approved:
            return PortfolioDecision(
                approved=False,
                final_risk_percent=0.0,
                reason_code=adversarial.reason_code,
                details=adversarial.details,
                timestamp_utc=now,
            )

        if open_symbols:
            pair = frozenset([technical_signal.symbol])
            for sym in open_symbols:
                candidate = frozenset([technical_signal.symbol, sym])
                correlation = self.CORRELATED_PAIRS.get(candidate)
                if correlation is not None and correlation >= self.CORRELATION_THRESHOLD:
                    return PortfolioDecision(
                        approved=False,
                        final_risk_percent=0.0,
                        reason_code="PM_CORRELATION_BLOCK",
                        details=f"correlated_with={sym} rho={correlation}",
                        timestamp_utc=now,
                    )

        if account_status.open_positions_count >= self.max_simultaneous_trades:
            return PortfolioDecision(
                approved=False,
                final_risk_percent=0.0,
                reason_code="PM_MAX_TRADES_REACHED",
                details=f"open_positions_count={account_status.open_positions_count}",
                timestamp_utc=now,
            )

        # Dynamic base risk: volatility-adjusted if regime data is available;
        # falls back to fixed base_risk when regime is None (backward compat / tests).
        if regime is not None:
            dynamic_base = self._compute_dynamic_risk(regime.volatility_state, regime.atr_ratio)
        else:
            dynamic_base = self.base_risk
        proposed_risk = dynamic_base * adversarial.risk_modifier
        if account_status.open_risk_percent + proposed_risk > self.max_total_risk:
            return PortfolioDecision(
                approved=False,
                final_risk_percent=0.0,
                reason_code="PM_EXPOSURE_LIMIT",
                details=(
                    f"open_risk_percent={account_status.open_risk_percent:.4f}, "
                    f"proposed={proposed_risk:.4f}, max={self.max_total_risk:.4f}"
                ),
                timestamp_utc=now,
            )

        return PortfolioDecision(
            approved=True,
            final_risk_percent=round(proposed_risk, 4),
            reason_code="PM_APPROVED",
            details="Portfolio constraints satisfied",
            timestamp_utc=now,
        )
