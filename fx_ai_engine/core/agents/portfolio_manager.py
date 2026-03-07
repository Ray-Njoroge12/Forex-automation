from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from config_microcapital import get_policy_config, read_fixed_risk_usd
from core.account_status import AccountStatus
from core.types import AdversarialDecision, PortfolioDecision, RegimeOutput, TechnicalSignal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixed-dollar risk mode
# ---------------------------------------------------------------------------
_FIXED_RISK_USD_ENV = "FIXED_RISK_USD"

FetchOHLC = Callable[[str, int, int], pd.DataFrame]


def _read_fixed_risk_usd() -> float | None:
    """Return runtime fixed-risk USD from env override or active policy."""
    return read_fixed_risk_usd()


class PortfolioManager:
    """Final pre-risk-engine allocator with exposure and stacking checks.

    Risk Modes
    ----------
    1. Fixed-USD mode (FIXED_RISK_USD env var set):
       Converts a fixed dollar amount to a percent using the live balance.
       e.g. FIXED_RISK_USD=10, balance=$100,000 → risk_percent = 0.0001 (0.01%)
       This mode bypasses ATR-ratio scaling to preserve the exact dollar floor.

    2. Percentage mode (default / SRS v1):
       Uses base_risk=3.2% scaled dynamically by ATR ratio and adversarial modifier.
    """

    # Static correlation map used as FALLBACK when live OHLC is unavailable.
    STATIC_CORRELATED_PAIRS: dict[frozenset, float] = {
        frozenset(["EURUSD", "GBPUSD"]): 0.82,
        frozenset(["EURUSD", "AUDUSD"]): 0.71,
        frozenset(["USDCHF", "EURUSD"]): 0.89,
        frozenset(["USDCAD", "AUDUSD"]): 0.76,
    }
    CORRELATION_THRESHOLD = 0.75

    # How many D1 candles to use for rolling correlation (5 days).
    CORRELATION_LOOKBACK = 5

    def __init__(
        self,
        fixed_risk_usd: float | None = None,
        fetch_ohlc: FetchOHLC | None = None,
        policy: dict | None = None,
    ):
        self.policy = get_policy_config() if policy is None else dict(policy)
        self.mode_id = self.policy["MODE_ID"]
        self.mode_label = self.policy["MODE_LABEL"]
        self.max_simultaneous_trades = self.policy["MAX_SIMULTANEOUS_TRADES"]

        # Fixed-USD mode: read from constructor arg first, then env, then None.
        if fixed_risk_usd is not None:
            self.fixed_risk_usd = fixed_risk_usd
        elif policy is not None:
            self.fixed_risk_usd = self.policy.get("FIXED_RISK_USD")
        else:
            self.fixed_risk_usd = _read_fixed_risk_usd()

        if self.fixed_risk_usd is not None:
            self.base_risk = 0.0  # unused in fixed-USD mode
            self.max_total_risk = 0.0  # computed dynamically per evaluate()
        else:
            self.base_risk = self.policy["BASE_RISK_PCT"]
            self.max_total_risk = self.policy["MAX_COMBINED_EXPOSURE"]

        # OHLC fetcher for dynamic correlation (optional; falls back to static).
        self._fetch_ohlc: FetchOHLC | None = fetch_ohlc

        # Cache: correlation matrix recomputed at most once per cycle.
        self._corr_matrix: dict[frozenset, float] | None = None
        self._corr_computed_at: datetime | None = None

    # ------------------------------------------------------------------
    # Dynamic Correlation
    # ------------------------------------------------------------------

    def _get_pair_correlation(self, sym_a: str, sym_b: str) -> float | None:
        """Return live Pearson correlation between two symbols.

        Uses a 5-day D1 close-price correlation if OHLC data is available.
        Falls back to the static map otherwise.
        """
        candidate = frozenset([sym_a, sym_b])

        # Try live correlation first.
        if self._fetch_ohlc is not None:
            try:
                corr = self._compute_pair_correlation(sym_a, sym_b)
                if corr is not None:
                    return corr
            except Exception as exc:
                logger.debug("Dynamic correlation failed for %s/%s: %s", sym_a, sym_b, exc)

        # Fallback to static.
        return self.STATIC_CORRELATED_PAIRS.get(candidate)

    def _compute_pair_correlation(self, sym_a: str, sym_b: str) -> float | None:
        """Compute Pearson correlation from D1 close prices."""
        if self._fetch_ohlc is None:
            return None

        TIMEFRAME_D1 = 16408  # MT5 TIMEFRAME_D1 constant

        df_a = self._fetch_ohlc(sym_a, TIMEFRAME_D1, self.CORRELATION_LOOKBACK + 5)
        df_b = self._fetch_ohlc(sym_b, TIMEFRAME_D1, self.CORRELATION_LOOKBACK + 5)

        if df_a.empty or df_b.empty:
            return None

        # Align by index (dates) and take last N bars.
        close_a = df_a["close"].tail(self.CORRELATION_LOOKBACK)
        close_b = df_b["close"].tail(self.CORRELATION_LOOKBACK)

        if len(close_a) < 3 or len(close_b) < 3:
            return None

        corr = close_a.reset_index(drop=True).corr(close_b.reset_index(drop=True))
        return abs(round(float(corr), 4)) if pd.notna(corr) else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_dynamic_risk(self, volatility_state: str, atr_ratio: float) -> float:
        """Scale base risk by ATR ratio — widen in low vol, shrink in high vol."""
        risk = self.base_risk / max(atr_ratio, 0.1)
        return round(max(0.015, min(risk, self.base_risk)), 4)

    def _fixed_risk_percent(self, balance: float) -> float:
        """Convert the fixed dollar amount to a fraction of the live balance."""
        if balance <= 0:
            return 0.0001
        return round(self.fixed_risk_usd / balance, 8)

    def _max_exposure_for_fixed(self, balance: float) -> float:
        """Portfolio ceiling in fixed-USD mode: 2× per-trade risk as a fraction."""
        if balance <= 0:
            return 0.0002
        return round((self.fixed_risk_usd * 2) / balance, 8)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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

        # Dynamic correlation block: reject highly correlated concurrent positions.
        if open_symbols:
            for sym in open_symbols:
                if sym == technical_signal.symbol:
                    continue
                correlation = self._get_pair_correlation(technical_signal.symbol, sym)
                if correlation is not None and correlation >= self.CORRELATION_THRESHOLD:
                    return PortfolioDecision(
                        approved=False,
                        final_risk_percent=0.0,
                        reason_code="PM_CORRELATION_BLOCK",
                        details=f"correlated_with={sym} rho={correlation:.4f}",
                        timestamp_utc=now,
                    )

        # Check for existing position on the same symbol to prevent double-entry.
        if open_symbols and technical_signal.symbol in open_symbols:
            return PortfolioDecision(
                approved=False,
                final_risk_percent=0.0,
                reason_code="PM_SYMBOL_ALREADY_OPEN",
                details=f"Already have position in {technical_signal.symbol}",
                timestamp_utc=now,
            )

        # Max simultaneous trades gate.
        if account_status.open_positions_count >= self.max_simultaneous_trades:
            return PortfolioDecision(
                approved=False,
                final_risk_percent=0.0,
                reason_code="PM_MAX_TRADES_REACHED",
                details=f"open_positions_count={account_status.open_positions_count}",
                timestamp_utc=now,
            )

        # ----------------------------------------------------------------
        # Compute proposed risk_percent depending on mode.
        # ----------------------------------------------------------------
        if self.fixed_risk_usd is not None:
            base_percent = self._fixed_risk_percent(account_status.balance)
            proposed_risk = round(base_percent * adversarial.risk_modifier, 8)
            max_exposure = self._max_exposure_for_fixed(account_status.balance)
            mode_label = f"mode={self.mode_id} fixed_usd=${self.fixed_risk_usd:.2f}"
        else:
            if regime is not None:
                dynamic_base = self._compute_dynamic_risk(
                    regime.volatility_state, regime.atr_ratio
                )
            else:
                dynamic_base = self.base_risk
            proposed_risk = round(dynamic_base * adversarial.risk_modifier, 4)
            max_exposure = self.max_total_risk
            mode_label = f"mode={self.mode_id} pct_base={self.base_risk:.4f}"

        # Portfolio ceiling check.
        if account_status.open_risk_percent + proposed_risk > max_exposure:
            return PortfolioDecision(
                approved=False,
                final_risk_percent=0.0,
                reason_code="PM_EXPOSURE_LIMIT",
                details=(
                    f"open_risk_percent={account_status.open_risk_percent:.6f}, "
                    f"proposed={proposed_risk:.6f}, max={max_exposure:.6f} "
                    f"[{mode_label}]"
                ),
                timestamp_utc=now,
            )

        return PortfolioDecision(
            approved=True,
            final_risk_percent=proposed_risk,
            reason_code="PM_APPROVED",
            details=f"Portfolio constraints satisfied [{mode_label}]",
            timestamp_utc=now,
        )

