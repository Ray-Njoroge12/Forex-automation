"""Meta-labeling subsystem for offline replay, labeling, and baseline training.

Phase 1 modules included in this repository:
    extract_data     - MT5 historical bar extraction to Parquet
    signal_replay    - Point-in-time candidate generation
    label            - Triple-barrier labeling
    validation       - Walk-forward and PurgedKFold utilities
    train_baseline   - Logistic-regression baseline harness
    adapters         - Scaffolding to reuse existing core agents safely
"""

from ml.meta_labeler.adapters import (  # noqa: F401
    CoreRegimeAgentAdapter,
    CoreTechnicalAgentAdapter,
    HistoricalOhlcProvider,
    build_core_agent_replay_adapters,
    map_engine_regime_to_replay,
    normalize_ohlcv_columns,
    normalize_regime_label_for_features,
)
from ml.meta_labeler.artifact_paths import (  # noqa: F401
    DEFAULT_REPLAY_ARTIFACT_ROOT,
    DEFAULT_TRAINING_ARTIFACT_ROOT,
    ReplayArtifactPaths,
    TrainingArtifactPaths,
    build_replay_artifact_paths,
    build_training_artifact_paths,
    generate_replay_run_id,
)
from ml.meta_labeler.offline_pipeline import (  # noqa: F401
    OfflineReplayConfig,
    OfflineReplayResult,
    run_offline_replay,
)
from ml.meta_labeler.training_pipeline import (  # noqa: F401
    OfflineTrainingConfig,
    OfflineTrainingResult,
    run_offline_training,
)
from ml.meta_labeler.shadow_runtime import (  # noqa: F401
    DEFAULT_CANARY_MODE,
    DEFAULT_CANARY_STAGE,
    DEFAULT_SHADOW_THRESHOLD,
    CanaryRuntimeConfig,
    CanaryRuntimeDecision,
    MetaLabelerShadowRuntime,
    ShadowRuntimeConfig,
    ShadowRuntimeDecision,
    evaluate_canary_decision,
    preserve_primary_route_decision,
    resolve_canary_runtime_config,
    resolve_shadow_runtime_config,
)

__all__ = [
    "CoreRegimeAgentAdapter",
    "CoreTechnicalAgentAdapter",
    "DEFAULT_REPLAY_ARTIFACT_ROOT",
    "DEFAULT_TRAINING_ARTIFACT_ROOT",
    "HistoricalOhlcProvider",
    "OfflineReplayConfig",
    "OfflineReplayResult",
    "OfflineTrainingConfig",
    "OfflineTrainingResult",
    "DEFAULT_CANARY_MODE",
    "DEFAULT_CANARY_STAGE",
    "DEFAULT_SHADOW_THRESHOLD",
    "CanaryRuntimeConfig",
    "CanaryRuntimeDecision",
    "ReplayArtifactPaths",
    "MetaLabelerShadowRuntime",
    "ShadowRuntimeConfig",
    "ShadowRuntimeDecision",
    "TrainingArtifactPaths",
    "build_core_agent_replay_adapters",
    "build_replay_artifact_paths",
    "build_training_artifact_paths",
    "generate_replay_run_id",
    "map_engine_regime_to_replay",
    "normalize_ohlcv_columns",
    "normalize_regime_label_for_features",
    "run_offline_replay",
    "run_offline_training",
    "evaluate_canary_decision",
    "preserve_primary_route_decision",
    "resolve_canary_runtime_config",
    "resolve_shadow_runtime_config",
]
