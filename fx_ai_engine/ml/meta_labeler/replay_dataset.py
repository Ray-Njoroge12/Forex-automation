"""Meta-labeler dataset from unified-replay candidates and trades (plan stage E3).

One row per candidate that became a trade. Features come from
``shadow_features`` on a ``PointInTimeFeed`` set to the candidate's decision
time with ``technical_timestamp_utc = decision_time``: the same closed-bar view
the live shadow runtime scores. (Feeding full frames to ``FeatureBuilder`` would
include the unclosed H1 bar.)

``train_from_replay_dataset`` fits the same LogisticRegression pipeline and
writes the same checkpoint dict as the offline training pipeline, so
``MetaLabelerShadowRuntime`` loads it unchanged. This module trains nothing by
itself; callers decide when to fit (development tests use tiny fixtures only).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ml.features import FEATURE_ORDER
from ml.meta_labeler.shadow_runtime import (
    ShadowFeatureBuildFailed,
    ShadowMarketDataUnavailable,
    ShadowRuntimeConfig,
    shadow_features,
)
from ml.meta_labeler.training_pipeline import _persist_baseline_checkpoint

META_COLUMNS = ("binary_label", "realized_r", "regime", "symbol", "strategy_id")


def _trade_candidate(trade: dict[str, Any], by_symbol: dict[str, list[Any]]):
    """The approved candidate behind ``trade``.

    Fills happen at the next bar open, which is the decision time unless the
    feed has a gap; so the match is the latest candidate of the same symbol
    with ``decision_time <= entry_time`` (equal in the normal case). No other
    candidate of that symbol can sit between approval and fill: a symbol with a
    pending entry is skipped until it fills.
    """
    entry = pd.Timestamp(trade["entry_time"])
    best = None
    for cand in by_symbol.get(str(trade["symbol"]), ()):
        if pd.Timestamp(cand.decision_time) <= entry:
            best = cand
        else:
            break
    return best


def build_replay_dataset(
    candidates: Sequence[Any],
    result: Any,
    frames: dict[str, dict[str, pd.DataFrame]],
    *,
    config: ShadowRuntimeConfig = ShadowRuntimeConfig(),
) -> pd.DataFrame:
    """Rows: ``FEATURE_ORDER`` + ``binary_label`` (net ``r_multiple > 0``) + ``realized_r``
    + ``regime`` + ``symbol`` + ``strategy_id``; index = decision time (UTC).

    Trades whose features cannot be built are left out and counted in
    ``df.attrs["skipped"]``.
    """
    from backtesting.unified_replay import PointInTimeFeed

    by_symbol: dict[str, list[Any]] = defaultdict(list)
    for cand in sorted(candidates, key=lambda c: pd.Timestamp(c.decision_time)):
        by_symbol[cand.symbol].append(cand)

    feed = PointInTimeFeed(frames)
    rows: list[np.ndarray] = []
    meta: list[dict[str, Any]] = []
    index: list[pd.Timestamp] = []
    skipped = 0
    for trade in result.trades:
        cand = _trade_candidate(trade, by_symbol)
        if cand is None:
            skipped += 1
            continue
        decision_time = pd.Timestamp(cand.decision_time).tz_convert("UTC")
        feed.set_time(decision_time)
        try:
            vector = shadow_features(feed.fetch_ohlc, cand.symbol, str(cand.regime.get("regime", "")),
                                     decision_time.isoformat(), config)
        except (ShadowMarketDataUnavailable, ShadowFeatureBuildFailed):
            skipped += 1
            continue
        realized = float(trade["r_multiple"])
        rows.append(vector.values)
        meta.append({"binary_label": int(realized > 0), "realized_r": realized,
                     "regime": str(cand.regime.get("regime", "")), "symbol": cand.symbol,
                     "strategy_id": str(getattr(cand, "strategy_id", trade.get("strategy_id", "")))})
        index.append(decision_time)

    features = pd.DataFrame(rows, columns=list(FEATURE_ORDER), dtype="float64") if rows else \
        pd.DataFrame(columns=list(FEATURE_ORDER), dtype="float64")
    df = pd.concat([features, pd.DataFrame(meta, columns=list(META_COLUMNS))], axis=1)
    df.index = pd.DatetimeIndex(index, tz="UTC", name="decision_time") if index else \
        pd.DatetimeIndex([], tz="UTC", name="decision_time")
    df.attrs["skipped"] = skipped
    return df


def train_from_replay_dataset(df: pd.DataFrame, checkpoint_path: Path, C: float = 0.5) -> dict[str, object]:
    """Fit the baseline LogisticRegression on ``df`` and write a shadow-loadable checkpoint.

    A single-class dataset writes a checkpoint without a model (``model_status
    = skipped_single_class``), as the offline pipeline does.
    """
    if df.empty:
        raise ValueError("replay dataset is empty")
    features = df[list(FEATURE_ORDER)].astype("float64")
    labels = df["binary_label"].astype("int64")
    symbols = sorted(set(df["symbol"].astype(str)))
    checkpoint_path = Path(checkpoint_path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    return _persist_baseline_checkpoint(checkpoint_path, ",".join(symbols), features, labels, C)
