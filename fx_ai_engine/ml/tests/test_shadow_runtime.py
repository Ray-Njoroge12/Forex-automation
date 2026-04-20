"""Tests for observe-only ML shadow runtime."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from config_microcapital import CORE_SRS_CONFIG, apply_runtime_experiment_config
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15
from ml.meta_labeler.shadow_runtime import (
    CanaryRuntimeConfig,
    MetaLabelerShadowRuntime,
    ShadowRuntimeConfig,
    evaluate_canary_decision,
    preserve_primary_route_decision,
    resolve_canary_runtime_config,
    resolve_shadow_runtime_config,
)


class _ConstantProbabilityModel:
    def __init__(self, probability: float) -> None:
        self._probability = float(probability)

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        rows = int(features.shape[0])
        p = np.full(rows, self._probability, dtype=np.float64)
        return np.column_stack((1.0 - p, p))


def _fetch_factory(m15_df: pd.DataFrame, h1_df: pd.DataFrame):
    def _fetch(_symbol: str, timeframe: int, candles: int) -> pd.DataFrame:
        _ = candles
        if timeframe == TIMEFRAME_M15:
            return m15_df.copy()
        if timeframe == TIMEFRAME_H1:
            return h1_df.copy()
        return pd.DataFrame()

    return _fetch


def test_shadow_runtime_disabled_returns_disabled(m15_bars, h1_bars) -> None:
    runtime = MetaLabelerShadowRuntime(
        config=ShadowRuntimeConfig(enabled=False),
        fetch_ohlc=_fetch_factory(m15_bars, h1_bars),
        model=_ConstantProbabilityModel(0.9),
    )

    decision = runtime.evaluate(
        symbol="EURUSD",
        trade_id="trade-1",
        regime_label="TRENDING_BULL",
        decision_time=datetime.now(timezone.utc),
        technical_timestamp_utc=m15_bars.index[-1].isoformat(),
    )

    assert decision.outcome == "DISABLED"
    assert decision.reason_code == "ML_SHADOW_DISABLED"
    assert decision.probability is None


def test_shadow_runtime_pass_vote_is_recorded(m15_bars, h1_bars) -> None:
    runtime = MetaLabelerShadowRuntime(
        config=ShadowRuntimeConfig(enabled=True, threshold=0.55),
        fetch_ohlc=_fetch_factory(m15_bars, h1_bars),
        model=_ConstantProbabilityModel(0.80),
    )

    decision = runtime.evaluate(
        symbol="EURUSD",
        trade_id="trade-2",
        regime_label="TRENDING_BULL",
        decision_time=datetime.now(timezone.utc),
        technical_timestamp_utc=m15_bars.index[-1].isoformat(),
    )

    assert decision.outcome == "PASS"
    assert decision.reason_code == "ML_SHADOW_PASS"
    assert decision.model_loaded is True
    assert decision.probability == pytest.approx(0.80)


def test_shadow_runtime_reject_vote_is_recorded(m15_bars, h1_bars) -> None:
    runtime = MetaLabelerShadowRuntime(
        config=ShadowRuntimeConfig(enabled=True, threshold=0.55),
        fetch_ohlc=_fetch_factory(m15_bars, h1_bars),
        model=_ConstantProbabilityModel(0.20),
    )

    decision = runtime.evaluate(
        symbol="EURUSD",
        trade_id="trade-3",
        regime_label="TRENDING_BEAR",
        decision_time=datetime.now(timezone.utc),
        technical_timestamp_utc=m15_bars.index[-1].isoformat(),
    )

    assert decision.outcome == "REJECT"
    assert decision.reason_code == "ML_SHADOW_REJECT"
    assert decision.probability == pytest.approx(0.20)


def test_shadow_runtime_bypasses_when_model_unavailable(m15_bars, h1_bars) -> None:
    runtime = MetaLabelerShadowRuntime(
        config=ShadowRuntimeConfig(enabled=True, threshold=0.55),
        fetch_ohlc=_fetch_factory(m15_bars, h1_bars),
        model=None,
    )

    decision = runtime.evaluate(
        symbol="EURUSD",
        trade_id="trade-4",
        regime_label="TRENDING_BULL",
        decision_time=datetime.now(timezone.utc),
        technical_timestamp_utc=m15_bars.index[-1].isoformat(),
    )

    assert decision.outcome == "BYPASS"
    assert decision.reason_code == "ML_SHADOW_MODEL_UNAVAILABLE"


def test_preserve_primary_route_decision_is_identity() -> None:
    outcomes = ("PASS", "REJECT", "BYPASS", "DISABLED")
    for primary_route_allowed in (True, False):
        for shadow_outcome in outcomes:
            assert (
                preserve_primary_route_decision(primary_route_allowed, shadow_outcome)
                == primary_route_allowed
            )


def test_resolve_shadow_runtime_config_reads_policy_experiment(tmp_path) -> None:
    checkpoint = tmp_path / "baseline_model.joblib"
    policy = {
        "EXPERIMENTS": {
            "META_LABELER_SHADOW": {
                "enabled": True,
                "threshold": 0.61,
                "model_path": str(checkpoint),
            }
        }
    }

    cfg = resolve_shadow_runtime_config(policy, env={})
    assert cfg.enabled is True
    assert cfg.threshold == pytest.approx(0.61)
    assert cfg.checkpoint_path == checkpoint


def test_resolve_shadow_runtime_config_env_overrides_policy(tmp_path) -> None:
    policy = {
        "EXPERIMENTS": {
            "META_LABELER_SHADOW": {
                "enabled": False,
                "threshold": 0.40,
                "model_path": "ignored.joblib",
            }
        }
    }
    override_path = tmp_path / "override.joblib"
    env = {
        "FX_META_LABELER_SHADOW_ENABLED": "1",
        "FX_META_LABELER_SHADOW_THRESHOLD": "0.72",
        "FX_META_LABELER_SHADOW_MODEL_PATH": str(override_path),
    }

    cfg = resolve_shadow_runtime_config(policy, env=env)
    assert cfg.enabled is True
    assert cfg.threshold == pytest.approx(0.72)
    assert cfg.checkpoint_path == override_path


@pytest.mark.parametrize(
    ("raw_threshold", "expected"),
    (("-0.1", 0.0), ("2.0", 1.0), ("not-a-number", 0.55)),
)
def test_resolve_shadow_runtime_config_threshold_is_bounded(
    raw_threshold: str,
    expected: float,
) -> None:
    policy = {"EXPERIMENTS": {"META_LABELER_SHADOW": {"enabled": False, "threshold": 0.55}}}
    cfg = resolve_shadow_runtime_config(
        policy,
        env={"FX_META_LABELER_SHADOW_THRESHOLD": raw_threshold},
    )
    assert cfg.threshold == pytest.approx(expected)


def test_runtime_experiment_config_wires_meta_labeler_shadow_toggle() -> None:
    env = {"FX_EXPERIMENT_META_LABELER_SHADOW": "1"}
    demo_policy = apply_runtime_experiment_config(CORE_SRS_CONFIG, run_mode="demo", env=env)
    smoke_policy = apply_runtime_experiment_config(CORE_SRS_CONFIG, run_mode="smoke", env=env)

    demo_cfg = demo_policy["EXPERIMENTS"]["META_LABELER_SHADOW"]
    smoke_cfg = smoke_policy["EXPERIMENTS"]["META_LABELER_SHADOW"]

    assert demo_cfg["enabled"] is True
    assert smoke_cfg["enabled"] is False


def test_resolve_canary_runtime_config_reads_policy_experiment() -> None:
    policy = {
        "EXPERIMENTS": {
            "META_LABELER_CANARY": {
                "enabled": True,
                "mode": "strict",
                "enforce_stage": "POST_HARD_RISK",
            }
        }
    }

    cfg = resolve_canary_runtime_config(policy, env={})
    assert cfg.enabled is True
    assert cfg.mode == "strict"
    assert cfg.enforce_stage == "POST_HARD_RISK"


def test_resolve_canary_runtime_config_env_overrides_policy() -> None:
    policy = {
        "EXPERIMENTS": {
            "META_LABELER_CANARY": {
                "enabled": False,
                "mode": "soft",
                "enforce_stage": "POST_HARD_RISK",
            }
        }
    }
    env = {
        "FX_META_LABELER_CANARY_ENABLED": "1",
        "FX_META_LABELER_CANARY_MODE": "strict",
        "FX_META_LABELER_CANARY_STAGE": "POST_HARD_RISK",
    }

    cfg = resolve_canary_runtime_config(policy, env=env)
    assert cfg.enabled is True
    assert cfg.mode == "strict"
    assert cfg.enforce_stage == "POST_HARD_RISK"


def test_resolve_canary_runtime_config_kill_switch_disables() -> None:
    policy = {
        "EXPERIMENTS": {
            "META_LABELER_CANARY": {
                "enabled": True,
                "mode": "strict",
                "enforce_stage": "POST_HARD_RISK",
            }
        }
    }
    cfg = resolve_canary_runtime_config(
        policy,
        env={"FX_META_LABELER_CANARY_KILL_SWITCH": "1"},
    )
    assert cfg.enabled is False
    assert cfg.kill_switch is True


def test_evaluate_canary_decision_soft_mode_observe_only() -> None:
    decision = evaluate_canary_decision(
        CanaryRuntimeConfig(enabled=True, mode="soft", enforce_stage="POST_HARD_RISK", kill_switch=False),
        stage="POST_HARD_RISK",
        shadow_enabled=True,
        shadow_outcome="REJECT",
        primary_gate_route=True,
    )
    assert decision.enforced is True
    assert decision.block_route is False
    assert decision.reason_code == "ML_CANARY_SOFT_OBSERVE"


def test_evaluate_canary_decision_strict_mode_blocks_shadow_reject() -> None:
    decision = evaluate_canary_decision(
        CanaryRuntimeConfig(enabled=True, mode="strict", enforce_stage="POST_HARD_RISK", kill_switch=False),
        stage="POST_HARD_RISK",
        shadow_enabled=True,
        shadow_outcome="REJECT",
        primary_gate_route=True,
    )
    assert decision.enforced is True
    assert decision.block_route is True
    assert decision.reason_code == "ML_CANARY_STRICT_BLOCK"


def test_evaluate_canary_decision_stage_mismatch_bypasses() -> None:
    decision = evaluate_canary_decision(
        CanaryRuntimeConfig(enabled=True, mode="strict", enforce_stage="POST_HARD_RISK", kill_switch=False),
        stage="PRE_ROUTE_FEASIBILITY",
        shadow_enabled=True,
        shadow_outcome="REJECT",
        primary_gate_route=True,
    )
    assert decision.enforced is False
    assert decision.block_route is False
    assert decision.reason_code == "ML_CANARY_STAGE_BYPASS"


def test_runtime_experiment_config_wires_meta_labeler_canary_toggle() -> None:
    env = {"FX_EXPERIMENT_META_LABELER_CANARY": "1"}
    demo_policy = apply_runtime_experiment_config(CORE_SRS_CONFIG, run_mode="demo", env=env)
    smoke_policy = apply_runtime_experiment_config(CORE_SRS_CONFIG, run_mode="smoke", env=env)

    demo_cfg = demo_policy["EXPERIMENTS"]["META_LABELER_CANARY"]
    smoke_cfg = smoke_policy["EXPERIMENTS"]["META_LABELER_CANARY"]

    assert demo_cfg["enabled"] is True
    assert smoke_cfg["enabled"] is False
