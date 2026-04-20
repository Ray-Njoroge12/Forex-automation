"""Signal replay — run existing agents over historical bars to produce
training candidates for the meta-labeler.

This is THE critical module for meta-labeling: the ML model learns which
of YOUR specific rules' signals succeed in production. If the replay
produces a different distribution than live, training is worthless.

Design:
    - Agent interfaces are defined as Protocols (duck typing).
    - Your real RegimeAgent / TechnicalAgent plug in with thin adapters
      if their signatures don't match exactly — see ADAPTER_NOTES below.
    - At each M15 bar, agents see ONLY data available at or before that
      bar close. Enforced via .loc[:bar_time] slicing.
    - Warmup period skips the first N bars to let slow indicators settle.

ADAPTER_NOTES — plugging in your existing agents:
    Your repo's RegimeAgent and TechnicalAgent are expected to satisfy
    these Protocols. If they don't, write a thin wrapper:

        class MyRegimeAdapter:
            def __init__(self, real_agent):
                self.real = real_agent

            def classify(self, h1_df):
                out = self.real.classify_from_series(h1_df["close"])
                return RegimeOutput(
                    regime=out.label,
                    confidence=out.confidence,
                    atr=out.atr,
                )
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Optional, Protocol

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# Data contracts
# ══════════════════════════════════════════════════════════════════════

# Regime labels the replay understands. Strings match the enum the
# real RegimeAgent produces. Extend as needed.
VALID_REGIMES: frozenset[str] = frozenset({
    "TREND_UP", "TREND_DOWN", "TREND", "TRENDING",
    "RANGE", "RANGING",
    "TRANSITION", "HIGH_VOL",
    "NO_REGIME",
})


@dataclass(frozen=True)
class RegimeOutput:
    """Output of a RegimeAgent classification.

    Attributes:
        regime: one of VALID_REGIMES. "NO_REGIME" causes replay to skip.
        confidence: [0, 1] confidence score from the classifier.
        atr: current H1 ATR (optional — may be None).
        extra: free-form dict for agent-specific metadata.
    """
    regime: str
    confidence: float
    atr: Optional[float] = None
    extra: Optional[dict] = None

    def __post_init__(self) -> None:
        if self.regime not in VALID_REGIMES:
            # Coerce to NO_REGIME rather than crash — forward-compatible
            # with agents that emit novel regime labels.
            object.__setattr__(self, "regime", "NO_REGIME")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")


@dataclass(frozen=True)
class TechnicalSignal:
    """Output of a TechnicalAgent evaluation.

    Attributes:
        direction: "BUY" or "SELL".
        entry: planned entry price.
        stop_loss: planned SL price.
        take_profit: planned TP price.
        atr: M15 ATR at the signal bar (for labeling).
        confidence: [0, 1] agent confidence.
        rule_name: identifier of which rule fired (for analysis).
    """
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    atr: float
    confidence: float = 1.0
    rule_name: str = ""

    def __post_init__(self) -> None:
        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be BUY or SELL, got {self.direction!r}")
        if self.atr <= 0:
            raise ValueError(f"atr must be positive, got {self.atr}")
        # Ensure SL / TP are on the correct sides of entry
        if self.direction == "BUY":
            if not (self.stop_loss < self.entry < self.take_profit):
                raise ValueError(
                    f"BUY signal requires SL < entry < TP. "
                    f"Got SL={self.stop_loss}, entry={self.entry}, TP={self.take_profit}"
                )
        else:  # SELL
            if not (self.take_profit < self.entry < self.stop_loss):
                raise ValueError(
                    f"SELL signal requires TP < entry < SL. "
                    f"Got SL={self.stop_loss}, entry={self.entry}, TP={self.take_profit}"
                )

    @property
    def risk_reward(self) -> float:
        """R:R ratio = |TP - entry| / |entry - SL|."""
        reward = abs(self.take_profit - self.entry)
        risk = abs(self.entry - self.stop_loss)
        return reward / risk if risk > 0 else 0.0


@dataclass(frozen=True)
class CandidateSignal:
    """A historical trade candidate that WOULD have been taken by the
    existing rules. Immutable record for training.

    Attributes:
        timestamp: M15 bar close timestamp (UTC).
        symbol: e.g. "EURUSD".
        direction: "BUY" or "SELL".
        entry / stop_loss / take_profit: price levels.
        atr: M15 ATR at signal time.
        regime: regime label at signal time.
        regime_confidence: regime agent's confidence.
        rule_name: which rule fired (if available).
        risk_reward: derived R:R ratio.
    """
    timestamp: pd.Timestamp
    symbol: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    atr: float
    regime: str
    regime_confidence: float
    rule_name: str = ""
    risk_reward: float = 0.0

    def to_dict(self) -> dict:
        """Serialize for Parquet / DataFrame storage."""
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "atr": self.atr,
            "regime": self.regime,
            "regime_confidence": self.regime_confidence,
            "rule_name": self.rule_name,
            "risk_reward": self.risk_reward,
        }


# ══════════════════════════════════════════════════════════════════════
# Agent protocols (duck typing)
# ══════════════════════════════════════════════════════════════════════

class RegimeClassifier(Protocol):
    """What the replay expects from a regime-classifier.

    Your existing RegimeAgent should implement this (directly or via an adapter).
    """

    def classify(self, h1_df: pd.DataFrame) -> RegimeOutput:
        """Given H1 bars up to and including 'now', classify the regime.
        MUST only use data in h1_df — no access to future bars.
        """
        ...


class TechnicalSignalGenerator(Protocol):
    """What the replay expects from a signal generator."""

    def evaluate(
        self,
        m15_df: pd.DataFrame,
        regime: RegimeOutput,
        symbol: str,
    ) -> Optional[TechnicalSignal]:
        """Given M15 bars up to and including 'now' and the current regime,
        return a TechnicalSignal or None if no setup fires.
        """
        ...


# ══════════════════════════════════════════════════════════════════════
# Replay loop
# ══════════════════════════════════════════════════════════════════════

def replay_signals(
    m15_df: pd.DataFrame,
    h1_df: pd.DataFrame,
    symbol: str,
    regime_agent: RegimeClassifier,
    technical_agent: TechnicalSignalGenerator,
    *,
    m15_warmup: int = 250,
    h1_warmup: int = 250,
    on_error: str = "skip",
    progress_callback=None,
) -> list[CandidateSignal]:
    """Walk bar-by-bar over M15 history, calling both agents point-in-time,
    and collect every candidate signal that would have been emitted live.

    Args:
        m15_df: full M15 OHLCV DataFrame (UTC DatetimeIndex).
        h1_df: full H1 OHLCV DataFrame covering at least the same range.
            Should start EARLIER than m15_df (by h1_warmup × 1h + buffer)
            so there's sufficient H1 history at every M15 timestamp.
        symbol: passed through to TechnicalSignalGenerator.evaluate().
        regime_agent: anything satisfying RegimeClassifier protocol.
        technical_agent: anything satisfying TechnicalSignalGenerator.
        m15_warmup: skip the first N M15 bars (indicator warmup).
        h1_warmup: skip bars where fewer than N H1 bars are available.
        on_error: "skip" (log and continue) or "raise" (fail loudly).
        progress_callback: optional callable(i, total, n_candidates).

    Returns:
        list of CandidateSignal, temporally sorted.

    Raises:
        TypeError on invalid inputs; RuntimeError on error_mode="raise".
    """
    if not isinstance(m15_df.index, pd.DatetimeIndex):
        raise TypeError("m15_df must have a DatetimeIndex")
    if not isinstance(h1_df.index, pd.DatetimeIndex):
        raise TypeError("h1_df must have a DatetimeIndex")
    if on_error not in ("skip", "raise"):
        raise ValueError(f"on_error must be 'skip' or 'raise', got {on_error!r}")
    if len(m15_df) <= m15_warmup:
        logger.warning(
            "m15_df has %d bars, warmup is %d — no candidates possible",
            len(m15_df), m15_warmup,
        )
        return []

    candidates: list[CandidateSignal] = []
    total = len(m15_df)
    skipped_warmup = 0
    skipped_regime = 0
    skipped_signal = 0
    errors = 0

    for i in range(m15_warmup, total):
        if progress_callback is not None and i % 5000 == 0:
            progress_callback(i, total, len(candidates))

        bar_time = m15_df.index[i]

        # Point-in-time slices — the contract with agents
        m15_slice = m15_df.iloc[: i + 1]
        h1_slice = h1_df.loc[:bar_time]

        if len(h1_slice) < h1_warmup:
            skipped_warmup += 1
            continue

        # 1. Regime classification on H1
        try:
            regime_out = regime_agent.classify(h1_slice)
        except Exception as exc:
            errors += 1
            if on_error == "raise":
                raise RuntimeError(
                    f"RegimeAgent failed at {bar_time}: {exc}"
                ) from exc
            logger.warning("RegimeAgent error at %s: %s", bar_time, exc)
            continue

        if regime_out.regime == "NO_REGIME":
            skipped_regime += 1
            continue

        # 2. Technical signal on M15 conditioned on regime
        try:
            signal = technical_agent.evaluate(m15_slice, regime_out, symbol)
        except Exception as exc:
            errors += 1
            if on_error == "raise":
                raise RuntimeError(
                    f"TechnicalAgent failed at {bar_time}: {exc}"
                ) from exc
            logger.warning("TechnicalAgent error at %s: %s", bar_time, exc)
            continue

        if signal is None:
            skipped_signal += 1
            continue

        candidates.append(CandidateSignal(
            timestamp=bar_time,
            symbol=symbol,
            direction=signal.direction,
            entry=float(signal.entry),
            stop_loss=float(signal.stop_loss),
            take_profit=float(signal.take_profit),
            atr=float(signal.atr),
            regime=regime_out.regime,
            regime_confidence=float(regime_out.confidence),
            rule_name=signal.rule_name,
            risk_reward=signal.risk_reward,
        ))

    logger.info(
        "Replay complete: %d candidates from %d bars. "
        "skipped: warmup=%d regime=%d signal=%d, errors=%d",
        len(candidates), total - m15_warmup,
        skipped_warmup, skipped_regime, skipped_signal, errors,
    )
    return candidates


def candidates_to_dataframe(candidates: list[CandidateSignal]) -> pd.DataFrame:
    """Convert candidate list to a typed, timestamp-indexed DataFrame.
    Useful as input to label.py and downstream feature builders.
    """
    if not candidates:
        return pd.DataFrame(
            columns=[
                "symbol", "direction", "entry", "stop_loss", "take_profit",
                "atr", "regime", "regime_confidence", "rule_name", "risk_reward",
            ],
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )
    records = [c.to_dict() for c in candidates]
    df = pd.DataFrame(records)
    df = df.set_index("timestamp").sort_index()
    return df


# ══════════════════════════════════════════════════════════════════════
# Reference stub implementations (for testing and documentation)
# ══════════════════════════════════════════════════════════════════════

class ConstantRegimeAgent:
    """Test stub: always returns the same regime with confidence 1.0.

    Useful as a minimal example of the RegimeClassifier protocol.
    """

    def __init__(self, regime: str = "TREND_UP", confidence: float = 1.0):
        self._regime = regime
        self._confidence = confidence

    def classify(self, h1_df: pd.DataFrame) -> RegimeOutput:
        return RegimeOutput(regime=self._regime, confidence=self._confidence)


class SimpleEMACrossTechnicalAgent:
    """Test stub: fires when M15 close crosses above the EMA(50) (BUY)
    or below (SELL), provided regime matches direction.

    NOT for production — just a realistic shape of signal for tests.
    """

    def __init__(self, ema_period: int = 50, atr_mult_sl: float = 1.0,
                 atr_mult_tp: float = 2.2):
        self.ema_period = ema_period
        self.atr_mult_sl = atr_mult_sl
        self.atr_mult_tp = atr_mult_tp

    def evaluate(
        self,
        m15_df: pd.DataFrame,
        regime: RegimeOutput,
        symbol: str,
    ) -> Optional[TechnicalSignal]:
        if len(m15_df) < max(self.ema_period, 14) + 2:
            return None
        close = m15_df["close"]
        ema = close.ewm(span=self.ema_period, adjust=False,
                         min_periods=self.ema_period).mean()
        if pd.isna(ema.iloc[-1]) or pd.isna(ema.iloc[-2]):
            return None
        # Simple ATR(14)
        high = m15_df["high"]
        low = m15_df["low"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low, (high - prev_close).abs(), (low - prev_close).abs()
        ], axis=1).max(axis=1)
        atr = float(tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1])
        if not np.isfinite(atr) or atr <= 0:
            return None

        c_now, c_prev = close.iloc[-1], close.iloc[-2]
        e_now, e_prev = ema.iloc[-1], ema.iloc[-2]

        # Bullish cross above EMA + regime allows longs
        if c_prev <= e_prev and c_now > e_now and regime.regime in (
            "TREND_UP", "TREND", "TRENDING"
        ):
            entry = float(c_now)
            return TechnicalSignal(
                direction="BUY",
                entry=entry,
                stop_loss=entry - self.atr_mult_sl * atr,
                take_profit=entry + self.atr_mult_tp * atr,
                atr=atr,
                rule_name="ema_cross_bull",
            )
        # Bearish cross below EMA + regime allows shorts
        if c_prev >= e_prev and c_now < e_now and regime.regime in (
            "TREND_DOWN", "TREND", "TRENDING"
        ):
            entry = float(c_now)
            return TechnicalSignal(
                direction="SELL",
                entry=entry,
                stop_loss=entry + self.atr_mult_sl * atr,
                take_profit=entry - self.atr_mult_tp * atr,
                atr=atr,
                rule_name="ema_cross_bear",
            )
        return None
