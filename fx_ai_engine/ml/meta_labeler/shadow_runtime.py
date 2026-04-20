"""Observe-only shadow runtime for offline-trained meta-labeler checkpoints.

The shadow runtime computes a pass/reject vote from the offline baseline model
but never changes live routing decisions. Primary gate ownership remains with
existing runtime controls.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15
from ml.features import FEATURE_SCHEMA_VERSION, FeatureBuilder
from ml.meta_labeler.adapters import (
    normalize_ohlcv_columns,
    normalize_regime_label_for_features,
)

logger = logging.getLogger(__name__)

SHADOW_ENABLED_ENV = "FX_META_LABELER_SHADOW_ENABLED"
SHADOW_MODEL_PATH_ENV = "FX_META_LABELER_SHADOW_MODEL_PATH"
SHADOW_THRESHOLD_ENV = "FX_META_LABELER_SHADOW_THRESHOLD"
CANARY_ENABLED_ENV = "FX_META_LABELER_CANARY_ENABLED"
CANARY_MODE_ENV = "FX_META_LABELER_CANARY_MODE"
CANARY_STAGE_ENV = "FX_META_LABELER_CANARY_STAGE"
CANARY_KILL_SWITCH_ENV = "FX_META_LABELER_CANARY_KILL_SWITCH"

DEFAULT_SHADOW_THRESHOLD = 0.55
DEFAULT_M15_LOOKBACK = 500
DEFAULT_H1_LOOKBACK = 500
DEFAULT_CANARY_MODE = "soft"
DEFAULT_CANARY_STAGE = "POST_HARD_RISK"

FetchOHLC = Callable[[str, int, int], pd.DataFrame]


def _parse_bool(raw: str | None) -> bool | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def preserve_primary_route_decision(primary_route_allowed: bool, shadow_outcome: str) -> bool:
    """Return primary routing decision unchanged.

    This helper intentionally enforces observe-only behavior regardless of
    shadow outcome values.
    """
    _ = shadow_outcome
    return bool(primary_route_allowed)


def resolve_shadow_runtime_config(
    policy: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> "ShadowRuntimeConfig":
    source = os.environ if env is None else env

    experiments = policy.get("EXPERIMENTS", {})
    experiment_cfg: Mapping[str, object] = {}
    if isinstance(experiments, Mapping):
        candidate = experiments.get("META_LABELER_SHADOW", {})
        if isinstance(candidate, Mapping):
            experiment_cfg = candidate

    enabled = bool(experiment_cfg.get("enabled", False))
    enabled_override = _parse_bool(source.get(SHADOW_ENABLED_ENV))
    if enabled_override is not None:
        enabled = enabled_override

    threshold = _coerce_threshold(
        source.get(SHADOW_THRESHOLD_ENV),
        fallback=float(experiment_cfg.get("threshold", DEFAULT_SHADOW_THRESHOLD) or DEFAULT_SHADOW_THRESHOLD),
    )

    model_path_raw = str(source.get(SHADOW_MODEL_PATH_ENV, "") or "").strip()
    if not model_path_raw:
        model_path_raw = str(experiment_cfg.get("model_path", "") or "").strip()
    checkpoint_path = Path(model_path_raw) if model_path_raw else None

    return ShadowRuntimeConfig(
        enabled=enabled,
        threshold=threshold,
        checkpoint_path=checkpoint_path,
    )


def _coerce_threshold(raw: str | None, *, fallback: float) -> float:
    try:
        value = float(str(raw or "").strip()) if str(raw or "").strip() else float(fallback)
    except (TypeError, ValueError):
        return float(fallback)
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _normalize_canary_mode(raw: str | None, *, fallback: str = DEFAULT_CANARY_MODE) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        value = str(fallback or DEFAULT_CANARY_MODE).strip().lower()
    if value in {"strict", "soft", "off", "disabled"}:
        return "off" if value == "disabled" else value
    return str(fallback or DEFAULT_CANARY_MODE).strip().lower()


@dataclass(frozen=True)
class CanaryRuntimeConfig:
    enabled: bool = False
    mode: str = DEFAULT_CANARY_MODE
    enforce_stage: str = DEFAULT_CANARY_STAGE
    kill_switch: bool = False


@dataclass(frozen=True)
class CanaryRuntimeDecision:
    stage: str
    mode: str
    enabled: bool
    enforced: bool
    block_route: bool
    reason_code: str
    details: str


def resolve_canary_runtime_config(
    policy: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> CanaryRuntimeConfig:
    source = os.environ if env is None else env

    experiments = policy.get("EXPERIMENTS", {})
    experiment_cfg: Mapping[str, object] = {}
    if isinstance(experiments, Mapping):
        candidate = experiments.get("META_LABELER_CANARY", {})
        if isinstance(candidate, Mapping):
            experiment_cfg = candidate

    enabled = bool(experiment_cfg.get("enabled", False))
    mode = _normalize_canary_mode(experiment_cfg.get("mode", DEFAULT_CANARY_MODE), fallback=DEFAULT_CANARY_MODE)
    enforce_stage = str(experiment_cfg.get("enforce_stage", DEFAULT_CANARY_STAGE) or DEFAULT_CANARY_STAGE).strip().upper()
    kill_switch = _parse_bool(source.get(CANARY_KILL_SWITCH_ENV)) is True

    enabled_override = _parse_bool(source.get(CANARY_ENABLED_ENV))
    if enabled_override is not None:
        enabled = enabled_override

    mode_override = str(source.get(CANARY_MODE_ENV, "") or "").strip()
    if mode_override:
        mode = _normalize_canary_mode(mode_override, fallback=mode)

    stage_override = str(source.get(CANARY_STAGE_ENV, "") or "").strip()
    if stage_override:
        enforce_stage = stage_override.upper()

    if mode == "off":
        enabled = False
    if kill_switch:
        enabled = False

    return CanaryRuntimeConfig(
        enabled=enabled,
        mode=mode,
        enforce_stage=enforce_stage,
        kill_switch=kill_switch,
    )


def evaluate_canary_decision(
    config: CanaryRuntimeConfig,
    *,
    stage: str,
    shadow_enabled: bool,
    shadow_outcome: str,
    primary_gate_route: bool,
) -> CanaryRuntimeDecision:
    stage_norm = str(stage or "").strip().upper()
    outcome_norm = str(shadow_outcome or "").strip().upper()

    if config.kill_switch:
        return CanaryRuntimeDecision(
            stage=stage_norm,
            mode=config.mode,
            enabled=False,
            enforced=False,
            block_route=False,
            reason_code="ML_CANARY_KILL_SWITCH",
            details="canary enforcement disabled by kill switch",
        )

    if not config.enabled:
        return CanaryRuntimeDecision(
            stage=stage_norm,
            mode=config.mode,
            enabled=False,
            enforced=False,
            block_route=False,
            reason_code="ML_CANARY_DISABLED",
            details="canary runtime disabled",
        )

    if config.enforce_stage != stage_norm:
        return CanaryRuntimeDecision(
            stage=stage_norm,
            mode=config.mode,
            enabled=True,
            enforced=False,
            block_route=False,
            reason_code="ML_CANARY_STAGE_BYPASS",
            details=f"stage={stage_norm} does not match enforce_stage={config.enforce_stage}",
        )

    if not shadow_enabled:
        return CanaryRuntimeDecision(
            stage=stage_norm,
            mode=config.mode,
            enabled=True,
            enforced=False,
            block_route=False,
            reason_code="ML_CANARY_SHADOW_DISABLED",
            details="shadow runtime disabled; canary cannot evaluate",
        )

    if config.mode == "soft":
        return CanaryRuntimeDecision(
            stage=stage_norm,
            mode=config.mode,
            enabled=True,
            enforced=True,
            block_route=False,
            reason_code="ML_CANARY_SOFT_OBSERVE",
            details=f"soft canary observe-only shadow_outcome={outcome_norm}",
        )

    should_block = bool(primary_gate_route and outcome_norm == "REJECT")
    if should_block:
        return CanaryRuntimeDecision(
            stage=stage_norm,
            mode=config.mode,
            enabled=True,
            enforced=True,
            block_route=True,
            reason_code="ML_CANARY_STRICT_BLOCK",
            details="strict canary blocked route due to shadow rejection",
        )

    return CanaryRuntimeDecision(
        stage=stage_norm,
        mode=config.mode,
        enabled=True,
        enforced=True,
        block_route=False,
        reason_code="ML_CANARY_STRICT_PASS",
        details=f"strict canary pass shadow_outcome={outcome_norm} primary_gate_route={primary_gate_route}",
    )


@dataclass(frozen=True)
class ShadowRuntimeConfig:
    enabled: bool = False
    threshold: float = DEFAULT_SHADOW_THRESHOLD
    checkpoint_path: Path | None = None
    m15_lookback: int = DEFAULT_M15_LOOKBACK
    h1_lookback: int = DEFAULT_H1_LOOKBACK


@dataclass(frozen=True)
class ShadowRuntimeDecision:
    decision_time_utc: str
    symbol: str
    trade_id: str | None
    outcome: str
    reason_code: str
    details: str
    probability: float | None
    threshold: float
    model_loaded: bool
    checkpoint_path: str | None
    feature_schema_version: str | None


class MetaLabelerShadowRuntime:
    """Evaluate meta-labeler baseline checkpoint in observe-only mode."""

    def __init__(
        self,
        *,
        config: ShadowRuntimeConfig,
        fetch_ohlc: FetchOHLC,
        model: Any | None = None,
    ) -> None:
        self.config = config
        self._fetch_ohlc = fetch_ohlc
        self._model = model
        self._feature_schema_version = FEATURE_SCHEMA_VERSION

        if self.config.enabled and self._model is None:
            self._load_checkpoint()

    @classmethod
    def from_policy(
        cls,
        *,
        policy: Mapping[str, object],
        fetch_ohlc: FetchOHLC,
        env: Mapping[str, str] | None = None,
    ) -> "MetaLabelerShadowRuntime":
        config = resolve_shadow_runtime_config(policy, env=env)
        return cls(config=config, fetch_ohlc=fetch_ohlc)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def evaluate(
        self,
        *,
        symbol: str,
        trade_id: str | None,
        regime_label: str,
        decision_time: datetime,
        technical_timestamp_utc: str,
    ) -> ShadowRuntimeDecision:
        symbol_norm = str(symbol or "").upper().strip()
        decision_iso = _to_utc_iso(decision_time)

        if not self.config.enabled:
            return ShadowRuntimeDecision(
                decision_time_utc=decision_iso,
                symbol=symbol_norm,
                trade_id=trade_id,
                outcome="DISABLED",
                reason_code="ML_SHADOW_DISABLED",
                details="shadow runtime disabled",
                probability=None,
                threshold=self.config.threshold,
                model_loaded=False,
                checkpoint_path=str(self.config.checkpoint_path) if self.config.checkpoint_path else None,
                feature_schema_version=self._feature_schema_version,
            )

        if self._model is None:
            return ShadowRuntimeDecision(
                decision_time_utc=decision_iso,
                symbol=symbol_norm,
                trade_id=trade_id,
                outcome="BYPASS",
                reason_code="ML_SHADOW_MODEL_UNAVAILABLE",
                details="shadow checkpoint not loaded",
                probability=None,
                threshold=self.config.threshold,
                model_loaded=False,
                checkpoint_path=str(self.config.checkpoint_path) if self.config.checkpoint_path else None,
                feature_schema_version=self._feature_schema_version,
            )

        m15_df = self._fetch_ohlc(symbol_norm, TIMEFRAME_M15, self.config.m15_lookback)
        h1_df = self._fetch_ohlc(symbol_norm, TIMEFRAME_H1, self.config.h1_lookback)
        if m15_df.empty or h1_df.empty:
            return ShadowRuntimeDecision(
                decision_time_utc=decision_iso,
                symbol=symbol_norm,
                trade_id=trade_id,
                outcome="BYPASS",
                reason_code="ML_SHADOW_MARKET_DATA_UNAVAILABLE",
                details="insufficient market data for shadow features",
                probability=None,
                threshold=self.config.threshold,
                model_loaded=True,
                checkpoint_path=str(self.config.checkpoint_path) if self.config.checkpoint_path else None,
                feature_schema_version=self._feature_schema_version,
            )

        try:
            m15_df = normalize_ohlcv_columns(m15_df).sort_index(kind="mergesort")
            h1_df = normalize_ohlcv_columns(h1_df).sort_index(kind="mergesort")
            feature_builder = FeatureBuilder(m15_df, h1_df, symbol_norm)
            feature_time = _resolve_feature_time(technical_timestamp_utc, m15_df)
            normalized_regime = normalize_regime_label_for_features(regime_label)
            vector = feature_builder.build(feature_time, regime_label=normalized_regime)
        except (ValueError, RuntimeError) as exc:
            return ShadowRuntimeDecision(
                decision_time_utc=decision_iso,
                symbol=symbol_norm,
                trade_id=trade_id,
                outcome="BYPASS",
                reason_code="ML_SHADOW_FEATURE_BUILD_FAILED",
                details=str(exc),
                probability=None,
                threshold=self.config.threshold,
                model_loaded=True,
                checkpoint_path=str(self.config.checkpoint_path) if self.config.checkpoint_path else None,
                feature_schema_version=self._feature_schema_version,
            )

        probability = self._predict_probability(vector.values)
        if probability is None:
            return ShadowRuntimeDecision(
                decision_time_utc=decision_iso,
                symbol=symbol_norm,
                trade_id=trade_id,
                outcome="BYPASS",
                reason_code="ML_SHADOW_INFERENCE_ERROR",
                details="shadow model inference failed",
                probability=None,
                threshold=self.config.threshold,
                model_loaded=True,
                checkpoint_path=str(self.config.checkpoint_path) if self.config.checkpoint_path else None,
                feature_schema_version=self._feature_schema_version,
            )

        outcome = "PASS" if probability >= self.config.threshold else "REJECT"
        reason_code = "ML_SHADOW_PASS" if outcome == "PASS" else "ML_SHADOW_REJECT"
        return ShadowRuntimeDecision(
            decision_time_utc=decision_iso,
            symbol=symbol_norm,
            trade_id=trade_id,
            outcome=outcome,
            reason_code=reason_code,
            details=f"prob={probability:.3f} threshold={self.config.threshold:.3f}",
            probability=probability,
            threshold=self.config.threshold,
            model_loaded=True,
            checkpoint_path=str(self.config.checkpoint_path) if self.config.checkpoint_path else None,
            feature_schema_version=self._feature_schema_version,
        )

    def _load_checkpoint(self) -> None:
        path = self.config.checkpoint_path
        if path is None:
            return
        if not path.exists():
            logger.info("Meta-labeler shadow checkpoint not found at %s", path)
            return

        try:
            import joblib  # type: ignore[import]
        except ImportError:
            logger.warning("joblib unavailable; shadow checkpoint cannot be loaded")
            return

        try:
            payload = joblib.load(path)
        except Exception as exc:  # pragma: no cover - defensive runtime path
            logger.warning("Failed loading shadow checkpoint at %s: %s", path, exc)
            return

        model: Any | None = None
        if isinstance(payload, Mapping):
            model = payload.get("model")
            schema_version = str(payload.get("schema_version") or "").strip()
            if schema_version:
                self._feature_schema_version = schema_version
        elif hasattr(payload, "predict_proba"):
            model = payload

        if model is None or not hasattr(model, "predict_proba"):
            logger.warning("Shadow checkpoint %s missing predict_proba model", path)
            return

        self._model = model

    def _predict_probability(self, features: np.ndarray) -> float | None:
        if self._model is None:
            return None
        if not hasattr(self._model, "predict_proba"):
            return None
        try:
            row = np.asarray(features, dtype=np.float32).reshape(1, -1)
            proba = self._model.predict_proba(row)
        except Exception:  # pragma: no cover - defensive runtime path
            return None

        arr = np.asarray(proba, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[0] != 1 or arr.shape[1] < 2:
            return None
        probability = float(arr[0, 1])
        if not np.isfinite(probability):
            return None
        if probability < 0.0:
            return 0.0
        if probability > 1.0:
            return 1.0
        return probability


def _resolve_feature_time(technical_timestamp_utc: str, m15_df: pd.DataFrame) -> pd.Timestamp:
    fallback = m15_df.index[-1]
    raw = str(technical_timestamp_utc or "").strip()
    if not raw:
        return fallback

    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = pd.Timestamp(raw)
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("UTC")
    parsed = parsed.tz_convert("UTC")

    if parsed in m15_df.index:
        return parsed
    scoped = m15_df.loc[:parsed]
    if scoped.empty:
        return fallback
    return scoped.index[-1]


def _to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()
