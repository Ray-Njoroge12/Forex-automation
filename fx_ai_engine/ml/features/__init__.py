"""Feature engineering module.

Exports:
    FeatureBuilder - main entry for computing 30-feature vectors
    FeatureVector - dataclass holding an ordered feature vector
    FEATURE_ORDER - frozen tuple of feature names (contract with ONNX)
    FEATURE_SCHEMA_VERSION - version string for audit
"""
from ml.features.builder import FeatureBuilder, FeatureVector
from ml.features.schema import FEATURE_ORDER, FEATURE_SCHEMA_VERSION

__all__ = [
    "FeatureBuilder",
    "FeatureVector",
    "FEATURE_ORDER",
    "FEATURE_SCHEMA_VERSION",
]
