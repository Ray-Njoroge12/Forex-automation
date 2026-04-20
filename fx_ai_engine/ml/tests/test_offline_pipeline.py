"""Tests for offline replay orchestration entrypoint."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from ml.meta_labeler.extract_data import save_parquet
from ml.meta_labeler.offline_pipeline import OfflineReplayConfig, run_offline_replay
from ml.meta_labeler.signal_replay import ConstantRegimeAgent, SimpleEMACrossTechnicalAgent


def _test_agents() -> tuple[ConstantRegimeAgent, SimpleEMACrossTechnicalAgent]:
    return (
        ConstantRegimeAgent(regime="TREND", confidence=0.8),
        SimpleEMACrossTechnicalAgent(ema_period=50),
    )


def test_run_offline_replay_writes_artifacts_from_in_memory_input(
    tmp_path,
    m15_bars,
    h1_bars,
) -> None:
    regime_agent, technical_agent = _test_agents()

    result = run_offline_replay(
        OfflineReplayConfig(
            symbol="EURUSD",
            output_root=tmp_path,
            run_id="in_memory_run",
            m15_df=m15_bars,
            h1_df=h1_bars,
            regime_agent=regime_agent,
            technical_agent=technical_agent,
            on_error="raise",
        )
    )

    assert result.run_id == "in_memory_run"
    assert result.paths.candidates_path.exists()
    assert result.paths.labeled_path.exists()
    assert result.paths.summary_path.exists()
    assert result.paths.run_manifest_path.exists()
    assert not result.paths.source_manifest_path.exists()

    candidate_df = pd.read_parquet(result.paths.candidates_path)
    labeled_df = pd.read_parquet(result.paths.labeled_path)
    assert len(candidate_df) == result.candidates_count
    assert len(labeled_df) == result.labeled_count
    assert result.label_metrics["total"] == result.labeled_count


def test_run_offline_replay_writes_source_manifest_for_parquet_input(
    tmp_path,
    m15_bars,
    h1_bars,
) -> None:
    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    data_dir.mkdir(parents=True)
    save_parquet(m15_bars, "EURUSD", "M15", data_dir)
    save_parquet(h1_bars, "EURUSD", "H1", data_dir)

    source_manifest = {
        "EURUSD_M15": {"rows": len(m15_bars)},
        "EURUSD_H1": {"rows": len(h1_bars)},
    }
    (data_dir / "manifest.json").write_text(json.dumps(source_manifest), encoding="utf-8")

    regime_agent, technical_agent = _test_agents()
    result = run_offline_replay(
        OfflineReplayConfig(
            symbol="EURUSD",
            data_dir=data_dir,
            output_root=out_dir,
            run_id="parquet_run",
            regime_agent=regime_agent,
            technical_agent=technical_agent,
        )
    )

    assert result.paths.source_manifest_path.exists()
    copied = json.loads(result.paths.source_manifest_path.read_text(encoding="utf-8"))
    assert copied == source_manifest


def test_run_offline_replay_requires_custom_agents_in_pairs(
    m15_bars,
    h1_bars,
) -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        run_offline_replay(
            OfflineReplayConfig(
                symbol="EURUSD",
                m15_df=m15_bars,
                h1_df=h1_bars,
                regime_agent=ConstantRegimeAgent("TREND"),
            )
        )
