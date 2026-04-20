"""Feature schema contract.

CRITICAL: FEATURE_ORDER is the positional contract for ONNX inference.
Any change to this order requires a full model retrain.
Any addition/removal of features requires a SCHEMA_VERSION bump.

ONNX models only know feature POSITIONS, not names. If the training
data has features in order [A, B, C] but inference passes [B, A, C],
predictions will be silently wrong. The schema version is stamped into
the ONNX metadata and verified at inference time.
"""
from __future__ import annotations

from typing import Final

FEATURE_SCHEMA_VERSION: Final[str] = "v1.0.0"

# The canonical ordered feature list.
# See fx_path_b_design.html §Phase 1 · Task 1.4 for formulas and justifications.
FEATURE_ORDER: Final[tuple[str, ...]] = (
    # ───── Group A · Price Dynamics (8) ─────
    "log_ret_1",
    "log_ret_5",
    "log_ret_20",
    "realized_vol_20",
    "atr_14_ratio",
    "atr_5_vs_20",
    "close_vs_ema50",
    "ema50_vs_ema200",
    # ───── Group B · Technical Indicators (8) ─────
    "rsi_14",
    "rsi_14_slope_5",
    "adx_14",
    "adx_slope_5",
    "bb_width_20",
    "bb_position_20",
    "macd_hist",
    "stoch_k_14",
    # ───── Group C · Microstructure (4) ─────
    "spread_pips",
    "spread_vs_atr",
    "volume_z_20",
    "volume_vs_ma20",
    # ───── Group D · Time & Session (6) ─────
    "hour_utc_sin",
    "hour_utc_cos",
    "day_of_week_sin",
    "is_london_session",
    "is_ny_session",
    "is_london_ny_overlap",
    # ───── Group E · Regime Context from H1 (4) ─────
    "h1_ema50_vs_ema200",
    "h1_adx_14",
    "h1_atr_vs_m15_atr",
    "regime_encoded",
)

assert len(FEATURE_ORDER) == 30, (
    f"Expected exactly 30 features, got {len(FEATURE_ORDER)}. "
    f"If you are adding features, bump FEATURE_SCHEMA_VERSION and retrain."
)
assert len(set(FEATURE_ORDER)) == 30, "Duplicate feature names in FEATURE_ORDER"

# Ordinal encoding of regime labels from RegimeAgent output.
# Values chosen so the model can learn an ordinal relationship:
#   0 = range/no clear trend
#   1 = transition / high-vol / uncertain
#   2 = confirmed trend (direction handled by separate features)
REGIME_ENCODING: Final[dict[str, int]] = {
    "RANGE": 0,
    "RANGING": 0,
    "NO_REGIME": 0,
    "UNKNOWN": 0,
    "TRANSITION": 1,
    "HIGH_VOL": 1,
    "TREND": 2,
    "TREND_UP": 2,
    "TREND_DOWN": 2,
    "TRENDING": 2,
}
