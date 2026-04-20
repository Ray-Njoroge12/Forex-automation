"""Offline replay orchestration for meta-labeler candidate generation.

This module wires the existing extraction, replay, and labeling blocks into a
single deterministic runner that persists run artifacts for auditability.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

import pandas as pd

from ml.meta_labeler.adapters import build_core_agent_replay_adapters, normalize_ohlcv_columns
from ml.meta_labeler.artifact_paths import (
    DEFAULT_REPLAY_ARTIFACT_ROOT,
    ReplayArtifactPaths,
    build_replay_artifact_paths,
)
from ml.meta_labeler.extract_data import DEFAULT_DATA_DIR, load_manifest, load_parquet
from ml.meta_labeler.label import DEFAULT_TTL_BARS_M15, label_all, label_summary, labeled_to_dataframe
from ml.meta_labeler.signal_replay import candidates_to_dataframe, replay_signals

if TYPE_CHECKING:
    from ml.meta_labeler.signal_replay import RegimeClassifier, TechnicalSignalGenerator


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfflineReplayConfig:
    """Inputs for running one offline replay + labeling pass."""

    symbol: str
    data_dir: Path = DEFAULT_DATA_DIR
    output_root: Path = DEFAULT_REPLAY_ARTIFACT_ROOT
    run_id: str | None = None
    m15_warmup: int = 250
    h1_warmup: int = 250
    ttl_bars: int = DEFAULT_TTL_BARS_M15
    min_lookahead: int | None = None
    on_error: str = "skip"
    policy: Mapping[str, object] | None = None
    spread_pips: float = 1.5
    m15_df: pd.DataFrame | None = None
    h1_df: pd.DataFrame | None = None
    regime_agent: "RegimeClassifier | None" = None
    technical_agent: "TechnicalSignalGenerator | None" = None


@dataclass(frozen=True)
class OfflineReplayResult:
    """Summary output from one offline replay run."""

    symbol: str
    run_id: str
    paths: ReplayArtifactPaths
    candidates_count: int
    labeled_count: int
    label_metrics: dict[str, float | int]


def run_offline_replay(config: OfflineReplayConfig) -> OfflineReplayResult:
    """Run extraction-compatible replay flow and persist deterministic artifacts."""
    symbol = _normalize_symbol(config.symbol)
    m15_df, h1_df = _resolve_input_dataframes(config, symbol)
    regime_agent, technical_agent = _resolve_agents(config, symbol, m15_df, h1_df)

    candidates = replay_signals(
        m15_df,
        h1_df,
        symbol,
        regime_agent,
        technical_agent,
        m15_warmup=config.m15_warmup,
        h1_warmup=config.h1_warmup,
        on_error=config.on_error,
    )
    labeled = label_all(
        candidates,
        m15_df,
        ttl_bars=config.ttl_bars,
        min_lookahead=config.min_lookahead,
    )

    candidate_df = candidates_to_dataframe(candidates)
    labeled_df = labeled_to_dataframe(labeled)
    metrics = label_summary(labeled)

    paths = build_replay_artifact_paths(config.output_root, run_id=config.run_id)
    candidate_df.to_parquet(paths.candidates_path, compression="zstd", index=True)
    labeled_df.to_parquet(paths.labeled_path, compression="zstd", index=True)

    source_manifest = _load_source_manifest_if_needed(config)
    if source_manifest:
        _write_json(paths.source_manifest_path, source_manifest)

    _write_json(
        paths.summary_path,
        {
            "symbol": symbol,
            "run_id": paths.run_id,
            "candidates": len(candidates),
            "labeled": len(labeled),
            **metrics,
        },
    )
    _write_json(
        paths.run_manifest_path,
        {
            "symbol": symbol,
            "run_id": paths.run_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "inputs": {
                "source": _input_source(config),
                "data_dir": str(config.data_dir),
            },
            "config": {
                "m15_warmup": config.m15_warmup,
                "h1_warmup": config.h1_warmup,
                "ttl_bars": config.ttl_bars,
                "min_lookahead": config.min_lookahead,
                "on_error": config.on_error,
                "spread_pips": config.spread_pips,
            },
            "counts": {
                "candidates": len(candidates),
                "labeled": len(labeled),
            },
            "artifacts": {
                "run_dir": str(paths.run_dir),
                "candidates": str(paths.candidates_path),
                "labeled": str(paths.labeled_path),
                "summary": str(paths.summary_path),
                "run_manifest": str(paths.run_manifest_path),
                "source_manifest": str(paths.source_manifest_path),
            },
        },
    )

    logger.info(
        "Offline replay complete for %s. candidates=%d labeled=%d run_id=%s",
        symbol,
        len(candidates),
        len(labeled),
        paths.run_id,
    )
    return OfflineReplayResult(
        symbol=symbol,
        run_id=paths.run_id,
        paths=paths,
        candidates_count=len(candidates),
        labeled_count=len(labeled),
        label_metrics={k: v for k, v in metrics.items() if isinstance(v, (int, float))},
    )


def _resolve_input_dataframes(
    config: OfflineReplayConfig,
    symbol: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    m15_df = config.m15_df.copy() if config.m15_df is not None else load_parquet(symbol, "M15", config.data_dir)
    h1_df = config.h1_df.copy() if config.h1_df is not None else load_parquet(symbol, "H1", config.data_dir)

    m15_df = normalize_ohlcv_columns(m15_df).sort_index()
    h1_df = normalize_ohlcv_columns(h1_df).sort_index()
    _validate_dataframe(m15_df, name="m15_df")
    _validate_dataframe(h1_df, name="h1_df")
    return m15_df, h1_df


def _resolve_agents(
    config: OfflineReplayConfig,
    symbol: str,
    m15_df: pd.DataFrame,
    h1_df: pd.DataFrame,
) -> tuple["RegimeClassifier", "TechnicalSignalGenerator"]:
    if (config.regime_agent is None) != (config.technical_agent is None):
        raise ValueError(
            "regime_agent and technical_agent must be provided together"
        )

    if config.regime_agent is not None and config.technical_agent is not None:
        return config.regime_agent, config.technical_agent

    regime_adapter, technical_adapter, _ = build_core_agent_replay_adapters(
        symbol,
        m15_df,
        h1_df,
        policy=config.policy,
        spread_pips=config.spread_pips,
    )
    return regime_adapter, technical_adapter


def _validate_dataframe(df: pd.DataFrame, *, name: str) -> None:
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError(f"{name} must have a DatetimeIndex")
    if df.empty:
        raise ValueError(f"{name} must not be empty")
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {sorted(missing)}")


def _normalize_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not value:
        raise ValueError("symbol is required")
    return value


def _input_source(config: OfflineReplayConfig) -> str:
    if config.m15_df is not None or config.h1_df is not None:
        return "in_memory"
    return "parquet"


def _load_source_manifest_if_needed(config: OfflineReplayConfig) -> dict:
    if _input_source(config) == "in_memory":
        return {}
    return load_manifest(config.data_dir)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    """CLI entrypoint for offline replay orchestration."""
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s - %(message)s",
    )
    parser = argparse.ArgumentParser(description="Run offline replay and labeling pipeline.")
    parser.add_argument("--symbol", required=True, help="Trading symbol, for example EURUSD")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Parquet input directory")
    parser.add_argument("--out", type=Path, default=DEFAULT_REPLAY_ARTIFACT_ROOT, help="Replay artifact root directory")
    parser.add_argument("--run-id", default=None, help="Optional explicit run id")
    parser.add_argument("--m15-warmup", type=int, default=250)
    parser.add_argument("--h1-warmup", type=int, default=250)
    parser.add_argument("--ttl-bars", type=int, default=DEFAULT_TTL_BARS_M15)
    parser.add_argument("--on-error", choices=["skip", "raise"], default="skip")
    parser.add_argument("--spread-pips", type=float, default=1.5)
    args = parser.parse_args()

    result = run_offline_replay(
        OfflineReplayConfig(
            symbol=args.symbol,
            data_dir=args.data_dir,
            output_root=args.out,
            run_id=args.run_id,
            m15_warmup=args.m15_warmup,
            h1_warmup=args.h1_warmup,
            ttl_bars=args.ttl_bars,
            on_error=args.on_error,
            spread_pips=args.spread_pips,
        )
    )
    print(f"run_id={result.run_id}")
    print(f"artifacts={result.paths.run_dir}")
    print(f"candidates={result.candidates_count}")
    print(f"labeled={result.labeled_count}")


if __name__ == "__main__":
    main()
