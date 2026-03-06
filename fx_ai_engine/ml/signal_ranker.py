"""Gradient-boosted signal ranker — predicts trade profitability from logged features.

The ranker learns from historical trades stored in the SQLite database.
It uses only features available *before* a trade is placed (no look-ahead).

Preferred model: XGBClassifier (falls back to RandomForest if xgboost
is not installed).

Prerequisite:
    ≥500 closed trades with r_multiple data in the trades table.

Training:
    python -m ml.signal_ranker --train

Inference (from code):
    ranker = SignalRanker()
    ranker.load()
    prob = ranker.predict_proba(features)
    if prob > 0.55:
        # route the trade

Features used:
    regime_confidence, rsi, atr_ratio, spread_pips, is_london_session,
    is_newyork_session, rate_differential, stop_pips, risk_reward,
    direction_buy, rsi_slope

Label:
    1 if r_multiple > 0 (profitable), else 0.

Model persistence uses joblib.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent / "signal_ranker_model.joblib"
MIN_TRAINING_SAMPLES = 500
PREDICT_THRESHOLD = 0.55  # minimum probability to route a signal

_FEATURE_NAMES = [
    "regime_confidence",
    "rsi",
    "atr_ratio",
    "spread_pips",
    "is_london_session",
    "is_newyork_session",
    "rate_differential",
    "stop_pips",
    "risk_reward",
    "direction_buy",
    "rsi_slope",
]


# ---------------------------------------------------------------------------
# DB data loading
# ---------------------------------------------------------------------------

def _load_training_data() -> tuple[np.ndarray, np.ndarray] | None:
    """Load feature matrix and labels from SQLite.  Returns None on failure."""
    try:
        import sqlite3

        db_path = Path(__file__).parent.parent / "database" / "trading_state.db"
        if not db_path.exists():
            logger.error("DB not found at %s", db_path)
            return None

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT regime_confidence, rsi_at_entry, atr_ratio, spread_entry,
                   is_london_session, is_newyork_session, rate_differential,
                   stop_loss, risk_reward, direction, r_multiple, rsi_slope
              FROM trades
             WHERE status LIKE 'CLOSED%'
               AND r_multiple IS NOT NULL
               AND regime_confidence IS NOT NULL
            """
        ).fetchall()
        conn.close()

        if len(rows) < MIN_TRAINING_SAMPLES:
            logger.warning(
                "Only %d training samples available; need ≥%d.",
                len(rows),
                MIN_TRAINING_SAMPLES,
            )
            return None

        X, y = [], []
        for row in rows:
            try:
                x_row = [
                    float(row["regime_confidence"] or 0),
                    float(row["rsi_at_entry"] or 50),
                    float(row["atr_ratio"] or 1),
                    float(row["spread_entry"] or 0),
                    float(row["is_london_session"] or 0),
                    float(row["is_newyork_session"] or 0),
                    float(row["rate_differential"] or 0),
                    float(row["stop_loss"] or 10),
                    float(row["risk_reward"] or 2),
                    1.0 if str(row["direction"]).upper() == "BUY" else 0.0,
                    float(row["rsi_slope"] or 0),
                ]
                label = 1 if float(row["r_multiple"]) > 0 else 0
                X.append(x_row)
                y.append(label)
            except (TypeError, ValueError):
                continue

        return np.array(X, dtype=np.float32), np.array(y, dtype=np.int32)

    except Exception as exc:
        logger.error("Failed to load training data: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SignalRanker:
    """XGBoost classifier for signal profitability prediction.

    Falls back to RandomForest if xgboost is not installed.

    Usage::

        ranker = SignalRanker()
        ranker.load()               # load saved model from disk
        prob = ranker.predict_proba({...})
        if prob >= PREDICT_THRESHOLD:
            route_signal()
    """

    def __init__(self):
        self._model: Any = None

    def is_ready(self) -> bool:
        """Return True if the model is loaded and ready for inference."""
        return self._model is not None

    def load(self) -> bool:
        """Load model from MODEL_PATH.  Returns False if file does not exist."""
        if not MODEL_PATH.exists():
            logger.info("No saved ranker model at %s — run with --train first.", MODEL_PATH)
            return False
        try:
            import joblib  # type: ignore[import]
            self._model = joblib.load(MODEL_PATH)
            logger.info("SignalRanker loaded from %s", MODEL_PATH)
            return True
        except Exception as exc:
            logger.warning("Failed to load SignalRanker model: %s", exc)
            return False

    def predict_proba(self, features: dict[str, float]) -> float:
        """Return probability that the signal will be profitable (0.0–1.0).

        Returns 0.5 (neutral) if the model is not loaded, ensuring the
        0.55 threshold lets signals through when the ranker is absent.
        """
        if self._model is None:
            return 0.5

        try:
            vec = np.array(
                [[features.get(f, 0.0) for f in _FEATURE_NAMES]],
                dtype=np.float32,
            )
            proba = self._model.predict_proba(vec)[0]
            # Class 1 = profitable.  predict_proba returns [prob_0, prob_1].
            return float(proba[1]) if len(proba) > 1 else 0.5
        except Exception as exc:
            logger.warning("SignalRanker inference error: %s", exc)
            return 0.5

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self) -> bool:
        """Train XGBoost (preferred) or RandomForest on historical trades and save model.

        Returns True on success.
        """
        # Try XGBoost first, fall back to RandomForest.
        use_xgb = False
        try:
            from xgboost import XGBClassifier  # type: ignore[import]
            use_xgb = True
        except ImportError:
            logger.info("xgboost not installed, falling back to RandomForest.")

        try:
            from sklearn.model_selection import cross_val_score  # type: ignore[import]
            import joblib  # type: ignore[import]
            if not use_xgb:
                from sklearn.ensemble import RandomForestClassifier  # type: ignore[import]
        except ImportError:
            logger.error(
                "Required packages missing. Run: pip install scikit-learn joblib xgboost"
            )
            return False

        data = _load_training_data()
        if data is None:
            return False

        X, y = data
        logger.info("Training SignalRanker on %d samples…", len(y))

        if use_xgb:
            clf = XGBClassifier(
                n_estimators=300,
                max_depth=5,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_weight=5,
                scale_pos_weight=1.0,
                use_label_encoder=False,
                eval_metric="logloss",
                random_state=42,
            )
        else:
            clf = RandomForestClassifier(
                n_estimators=200,
                max_depth=6,
                min_samples_leaf=10,
                class_weight="balanced",
                random_state=42,
            )

        clf.fit(X, y)

        # Cross-validated accuracy for diagnostics.
        scores = cross_val_score(clf, X, y, cv=5, scoring="roc_auc")
        logger.info("CV ROC-AUC: %.3f ± %.3f", scores.mean(), scores.std())

        joblib.dump(clf, MODEL_PATH)
        logger.info("Model saved to %s", MODEL_PATH)
        self._model = clf

        # Feature importance report.
        if use_xgb:
            importances_arr = clf.feature_importances_
        else:
            importances_arr = clf.feature_importances_

        importances = sorted(
            zip(_FEATURE_NAMES, importances_arr),
            key=lambda t: t[1],
            reverse=True,
        )
        print("Feature importances:")
        for name, imp in importances:
            print(f"  {name:<25} {imp:.4f}")

        return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FX AI Engine signal ranker")
    parser.add_argument("--train", action="store_true", help="Train on closed trades in DB")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()

    if args.train:
        ranker = SignalRanker()
        success = ranker.train()
        return 0 if success else 1

    print("Usage: python -m ml.signal_ranker --train")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
