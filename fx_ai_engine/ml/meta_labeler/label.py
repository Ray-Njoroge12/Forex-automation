"""Triple-barrier labeling (Lopez de Prado).

For each historical candidate signal, forward-simulate bar-by-bar until
one of three barriers is hit:
    1. take-profit (TP) → label WIN (+1)
    2. stop-loss (SL)   → label LOSS (0)
    3. time-to-live expires → label TIMEOUT (exit at last bar's close)

For binary classification we reduce {WIN, LOSS, TIMEOUT} → {1, 0}
where WIN = 1 and everything else = 0. This is intentional: we don't want
the model to learn to hold positions hoping for a timeout recovery.

Same-bar ambiguity:
    If both SL and TP appear hit inside the same bar's high/low range,
    we conservatively label it as LOSS. In reality stops tend to fill
    before targets due to market impact and the SL is typically reached
    first during a volatile bar. This conservative choice reduces
    overfitting and matches production behavior under slippage.

To disambiguate properly you'd need tick data for each bar — not worth it
for M15 labeling where the accuracy gain is marginal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from ml.meta_labeler.signal_replay import CandidateSignal, candidates_to_dataframe

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Outcome enum + labeled candidate record
# ══════════════════════════════════════════════════════════════════════

class LabelOutcome(IntEnum):
    """Reason a candidate was closed."""
    LOSS = 0        # stop loss hit first
    WIN = 1         # take profit hit first
    TIMEOUT = 2     # neither hit within TTL

    @property
    def is_win(self) -> bool:
        """Binary target for meta-labeling: only WIN counts as positive."""
        return self is LabelOutcome.WIN


@dataclass(frozen=True)
class LabeledCandidate:
    """A historical candidate after forward simulation.

    Attributes:
        candidate: the original CandidateSignal.
        outcome: LOSS / WIN / TIMEOUT.
        bars_held: number of M15 bars the trade was open.
        exit_price: price at which the trade closed.
        exit_time: timestamp of the exit bar.
        realized_r: PnL expressed in R-multiples (PnL / initial risk).
            +X.Y for wins, -1.0 for stops, positive/negative for timeouts.
    """
    candidate: CandidateSignal
    outcome: LabelOutcome
    bars_held: int
    exit_price: float
    exit_time: pd.Timestamp
    realized_r: float

    @property
    def binary_label(self) -> int:
        """1 if WIN, 0 if LOSS or TIMEOUT."""
        return 1 if self.outcome.is_win else 0

    def to_dict(self) -> dict:
        return {
            **self.candidate.to_dict(),
            "outcome": self.outcome.name,
            "binary_label": self.binary_label,
            "bars_held": self.bars_held,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time,
            "realized_r": self.realized_r,
        }


# ══════════════════════════════════════════════════════════════════════
# Labeling
# ══════════════════════════════════════════════════════════════════════

DEFAULT_TTL_BARS_M15: int = 24  # 24 × 15min = 6 hours


def triple_barrier_label(
    candidate: CandidateSignal,
    future_bars: pd.DataFrame,
    ttl_bars: int = DEFAULT_TTL_BARS_M15,
) -> LabeledCandidate:
    """Label a single candidate by forward-simulating its three barriers.

    Args:
        candidate: the CandidateSignal to label.
        future_bars: OHLCV DataFrame with bars STRICTLY AFTER
            candidate.timestamp. Must have columns 'high', 'low', 'close'.
        ttl_bars: max bars to hold before TIMEOUT.

    Returns:
        LabeledCandidate with outcome determined.

    Raises:
        ValueError: if future_bars is empty or missing required columns.
    """
    if not {"high", "low", "close"}.issubset(future_bars.columns):
        raise ValueError("future_bars must have high, low, close columns")
    if len(future_bars) == 0:
        raise ValueError("future_bars is empty — cannot label")
    if ttl_bars < 1:
        raise ValueError(f"ttl_bars must be >= 1, got {ttl_bars}")

    direction = candidate.direction
    entry = float(candidate.entry)
    sl = float(candidate.stop_loss)
    tp = float(candidate.take_profit)
    initial_risk = abs(entry - sl)
    if initial_risk <= 0:
        raise ValueError(
            f"Candidate at {candidate.timestamp} has zero risk "
            f"(entry={entry}, sl={sl})"
        )

    lookahead = future_bars.iloc[:ttl_bars]

    for offset, (bar_time, bar) in enumerate(lookahead.iterrows(), start=1):
        hi = float(bar["high"])
        lo = float(bar["low"])

        if direction == "BUY":
            hit_sl = lo <= sl
            hit_tp = hi >= tp
            if hit_sl and hit_tp:
                # Ambiguous → conservative LOSS
                return LabeledCandidate(
                    candidate=candidate,
                    outcome=LabelOutcome.LOSS,
                    bars_held=offset,
                    exit_price=sl,
                    exit_time=bar_time,
                    realized_r=-1.0,
                )
            if hit_sl:
                return LabeledCandidate(
                    candidate=candidate,
                    outcome=LabelOutcome.LOSS,
                    bars_held=offset,
                    exit_price=sl,
                    exit_time=bar_time,
                    realized_r=-1.0,
                )
            if hit_tp:
                reward = tp - entry
                return LabeledCandidate(
                    candidate=candidate,
                    outcome=LabelOutcome.WIN,
                    bars_held=offset,
                    exit_price=tp,
                    exit_time=bar_time,
                    realized_r=reward / initial_risk,
                )
        else:  # SELL
            hit_sl = hi >= sl
            hit_tp = lo <= tp
            if hit_sl and hit_tp:
                return LabeledCandidate(
                    candidate=candidate,
                    outcome=LabelOutcome.LOSS,
                    bars_held=offset,
                    exit_price=sl,
                    exit_time=bar_time,
                    realized_r=-1.0,
                )
            if hit_sl:
                return LabeledCandidate(
                    candidate=candidate,
                    outcome=LabelOutcome.LOSS,
                    bars_held=offset,
                    exit_price=sl,
                    exit_time=bar_time,
                    realized_r=-1.0,
                )
            if hit_tp:
                reward = entry - tp
                return LabeledCandidate(
                    candidate=candidate,
                    outcome=LabelOutcome.WIN,
                    bars_held=offset,
                    exit_price=tp,
                    exit_time=bar_time,
                    realized_r=reward / initial_risk,
                )

    # No barrier hit within TTL → TIMEOUT, exit at last bar's close
    last_bar = lookahead.iloc[-1]
    exit_price = float(last_bar["close"])
    if direction == "BUY":
        pnl = exit_price - entry
    else:
        pnl = entry - exit_price

    return LabeledCandidate(
        candidate=candidate,
        outcome=LabelOutcome.TIMEOUT,
        bars_held=len(lookahead),
        exit_price=exit_price,
        exit_time=lookahead.index[-1],
        realized_r=pnl / initial_risk,
    )


def label_all(
    candidates: Iterable[CandidateSignal],
    m15_bars: pd.DataFrame,
    ttl_bars: int = DEFAULT_TTL_BARS_M15,
    min_lookahead: Optional[int] = None,
) -> list[LabeledCandidate]:
    """Label a list of candidates against a full M15 bar DataFrame.

    For each candidate, pulls the future bars strictly after its
    timestamp and runs triple_barrier_label.

    Args:
        candidates: iterable of CandidateSignal.
        m15_bars: full M15 OHLCV DataFrame covering all candidate times.
            Must have a UTC DatetimeIndex.
        ttl_bars: see triple_barrier_label.
        min_lookahead: skip candidates with fewer than this many future
            bars (i.e. near the end of history). Defaults to ttl_bars.

    Returns:
        list of LabeledCandidate, in original order, skipping any
        candidate that couldn't be labeled due to insufficient lookahead.
    """
    if not isinstance(m15_bars.index, pd.DatetimeIndex):
        raise TypeError("m15_bars must have a DatetimeIndex")

    if min_lookahead is None:
        min_lookahead = ttl_bars

    labeled: list[LabeledCandidate] = []
    skipped_no_future = 0

    for cand in candidates:
        # Slice strictly AFTER the candidate timestamp (no look-ahead:
        # the bar at t is the decision bar, not available for exit)
        future = m15_bars.loc[m15_bars.index > cand.timestamp]
        if len(future) < min_lookahead:
            skipped_no_future += 1
            continue
        try:
            result = triple_barrier_label(cand, future, ttl_bars=ttl_bars)
        except ValueError as e:
            logger.warning(
                "Skipping unlabelable candidate at %s: %s",
                cand.timestamp, e,
            )
            continue
        labeled.append(result)

    logger.info(
        "Labeled %d candidates (skipped %d due to insufficient lookahead)",
        len(labeled), skipped_no_future,
    )
    return labeled


def labeled_to_dataframe(labeled: list[LabeledCandidate]) -> pd.DataFrame:
    """Convert labeled candidates to a flat, typed DataFrame indexed by
    the original candidate timestamp.
    """
    if not labeled:
        return pd.DataFrame()
    records = [lc.to_dict() for lc in labeled]
    df = pd.DataFrame(records)
    df = df.set_index("timestamp").sort_index()
    return df


# ══════════════════════════════════════════════════════════════════════
# Summary statistics — useful for sanity checking
# ══════════════════════════════════════════════════════════════════════

def label_summary(labeled: list[LabeledCandidate]) -> dict:
    """Summary stats for sanity checking label quality.

    Use this after label_all() to verify the distribution of outcomes
    before proceeding to training.
    """
    if not labeled:
        return {
            "total": 0, "wins": 0, "losses": 0, "timeouts": 0,
            "win_rate": 0.0, "binary_positive_rate": 0.0,
            "mean_realized_r": 0.0, "mean_bars_held": 0.0,
        }

    outcomes = [lc.outcome for lc in labeled]
    n = len(outcomes)
    wins = sum(1 for o in outcomes if o is LabelOutcome.WIN)
    losses = sum(1 for o in outcomes if o is LabelOutcome.LOSS)
    timeouts = sum(1 for o in outcomes if o is LabelOutcome.TIMEOUT)

    rs = np.array([lc.realized_r for lc in labeled])
    bars = np.array([lc.bars_held for lc in labeled])
    binary_positive = wins  # TIMEOUT treated as 0 for binary target

    return {
        "total": n,
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "win_rate": wins / n,
        "binary_positive_rate": binary_positive / n,
        "mean_realized_r": float(rs.mean()),
        "median_realized_r": float(np.median(rs)),
        "mean_bars_held": float(bars.mean()),
    }
