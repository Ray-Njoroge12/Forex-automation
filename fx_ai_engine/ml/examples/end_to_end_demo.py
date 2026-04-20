"""End-to-end Phase 1 demo — proves every module integrates correctly.

Pipeline:
    1. Synthesize historical M15 + H1 bars (stand-in for MT5 data)
    2. Replay a stub RegimeAgent + TechnicalAgent → candidates
    3. Triple-barrier label each candidate
    4. Build 30-feature vectors for each labeled candidate
    5. Generate walk-forward folds
    6. Train logistic regression baseline
    7. Report acceptance metrics

Run from the repo root:
    python -m ml.examples.end_to_end_demo
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Make the package importable when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ml.features.builder import FeatureBuilder
from ml.features.schema import FEATURE_ORDER
from ml.meta_labeler.label import label_all, label_summary, labeled_to_dataframe
from ml.meta_labeler.signal_replay import (
    ConstantRegimeAgent,
    SimpleEMACrossTechnicalAgent,
    candidates_to_dataframe,
    replay_signals,
)
from ml.meta_labeler.train_baseline import train_baseline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s - %(message)s",
)


def synthesize_market_data(
    n_h1: int = 10000,      # ~1.1 years of H1
    n_m15: int = 15000,     # ~5 months of M15 — small enough to finish fast
    start_price: float = 1.1000,
    drift: float = 0.00003,
    volatility: float = 0.0008,
    seed: int = 42,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate synthetic OHLCV bars that LOOK like forex."""
    rng = np.random.default_rng(seed)

    def _make(n: int, freq: str, start: str) -> pd.DataFrame:
        idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
        log_rets = rng.normal(drift, volatility, size=n)
        closes = start_price * np.exp(log_rets.cumsum())
        ranges = np.abs(rng.normal(0, 0.001 * start_price, size=n))
        highs = closes + ranges * 0.6
        lows = closes - ranges * 0.4
        opens = np.concatenate(([start_price], closes[:-1]))
        highs = np.maximum.reduce([highs, opens, closes])
        lows = np.minimum.reduce([lows, opens, closes])
        return pd.DataFrame(
            {
                "open": opens, "high": highs, "low": lows, "close": closes,
                "volume": rng.integers(500, 5000, size=n),
                "spread": rng.integers(5, 25, size=n).astype("int32"),
            },
            index=idx,
        )

    # H1 starts earlier than M15 so there's always sufficient H1 history
    h1 = _make(n_h1, "1h", start="2022-01-01 00:00")
    m15 = _make(n_m15, "15min", start="2022-07-01 00:00")
    return m15, h1


def main() -> None:
    print("\n" + "=" * 70)
    print("PHASE 1 END-TO-END DEMO")
    print("=" * 70)

    # ─────────────────────────────────────────────────────────────────
    # 1. Synthesize market data
    # ─────────────────────────────────────────────────────────────────
    print("\n[1/7] Synthesizing market data...")
    m15, h1 = synthesize_market_data()
    print(f"  M15: {len(m15):,} bars from {m15.index[0]} to {m15.index[-1]}")
    print(f"  H1:  {len(h1):,} bars from {h1.index[0]} to {h1.index[-1]}")

    # ─────────────────────────────────────────────────────────────────
    # 2. Replay signals
    # ─────────────────────────────────────────────────────────────────
    print("\n[2/7] Replaying historical signals...")
    # Alternating regime to produce both BUY and SELL candidates
    regime_agent = ConstantRegimeAgent(regime="TREND", confidence=0.8)
    technical_agent = SimpleEMACrossTechnicalAgent(ema_period=50)
    candidates = replay_signals(
        m15, h1, "EURUSD",
        regime_agent, technical_agent,
        m15_warmup=250,
    )
    print(f"  Generated {len(candidates):,} candidates")
    if len(candidates) < 200:
        print("  WARNING: too few candidates for meaningful evaluation.")

    # ─────────────────────────────────────────────────────────────────
    # 3. Triple-barrier labeling
    # ─────────────────────────────────────────────────────────────────
    print("\n[3/7] Labeling candidates (triple-barrier)...")
    labeled = label_all(candidates, m15, ttl_bars=24)
    summary = label_summary(labeled)
    print(f"  Labeled {summary['total']:,} candidates")
    print(f"  Wins:     {summary['wins']:>5} ({summary['win_rate']:.1%})")
    print(f"  Losses:   {summary['losses']:>5}")
    print(f"  Timeouts: {summary['timeouts']:>5}")
    print(f"  Binary positive rate: {summary['binary_positive_rate']:.1%}")
    print(f"  Mean realized R: {summary['mean_realized_r']:+.3f}")

    # ─────────────────────────────────────────────────────────────────
    # 4. Build features
    # ─────────────────────────────────────────────────────────────────
    print("\n[4/7] Building 30-feature vectors...")
    fb = FeatureBuilder(m15, h1, "EURUSD")
    feature_rows = []
    labels = []
    realized_r = []
    timestamps = []
    errors = 0
    for lc in labeled:
        try:
            fv = fb.build(lc.candidate.timestamp, regime_label=lc.candidate.regime)
            feature_rows.append(fv.values)
            labels.append(lc.binary_label)
            realized_r.append(lc.realized_r)
            timestamps.append(lc.candidate.timestamp)
        except (ValueError, RuntimeError) as e:
            errors += 1
            continue
    if errors:
        print(f"  {errors} candidates skipped (insufficient history)")
    X = pd.DataFrame(feature_rows, columns=list(FEATURE_ORDER),
                      index=pd.DatetimeIndex(timestamps, tz="UTC"))
    y = pd.Series(labels, index=X.index)
    r = pd.Series(realized_r, index=X.index)
    print(f"  Feature matrix: {X.shape[0]:,} rows × {X.shape[1]} features")
    print(f"  Label distribution: {y.value_counts().to_dict()}")

    if len(X) < 500:
        print("\n  Not enough samples to proceed with training. Exiting.")
        return

    # ─────────────────────────────────────────────────────────────────
    # 5 & 6. Walk-forward validation + baseline training
    # ─────────────────────────────────────────────────────────────────
    print("\n[5-6/7] Training logistic regression baseline with walk-forward CV...")
    result = train_baseline(
        X, y, r,
        train_months=3, test_months=1, step_months=1,  # short windows for demo
    )

    # ─────────────────────────────────────────────────────────────────
    # 7. Report
    # ─────────────────────────────────────────────────────────────────
    print("\n[7/7] Results")
    print("-" * 70)
    print(result.summary())
    print("-" * 70)

    # Per-fold breakdown
    print("\nPer-fold metrics:")
    print(f"  {'fold':>4}  {'train':>6}  {'test':>5}  {'sharpe':>8}  {'auc':>6}  {'prec':>6}  {'n_app':>6}")
    for fr in result.fold_results:
        print(
            f"  {fr.fold_index:>4}  {fr.train_size:>6}  {fr.test_size:>5}  "
            f"{fr.sharpe:>8.3f}  {fr.auc:>6.3f}  "
            f"{fr.precision:>6.3f}  {fr.n_approved:>6}"
        )

    print("\n" + "=" * 70)
    print("DEMO COMPLETE — Phase 1 pipeline works end-to-end")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
