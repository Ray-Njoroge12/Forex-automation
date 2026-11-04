"""run_replay wires the R1/R2 gate, R4 cost stress and R5 Monte Carlo into every report (ADR 0003)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backtesting import run_replay
from backtesting.unified_replay import load_csv_frames
from research import bias_audit
from research.authenticity_gate import NOT_EVIDENCE_LINE, PASS_LINE
from tests.replay_fixtures import FIXTURE_SYMBOLS, write_fixture_csvs

STEM = "baseline__policy__srs__base"


@pytest.fixture()
def replay_env(tmp_path, monkeypatch):
    data_dir = write_fixture_csvs(tmp_path / "data")
    frames = load_csv_frames(data_dir, symbols=FIXTURE_SYMBOLS)
    start = max(frames[s]["M15"].index[0] for s in FIXTURE_SYMBOLS) + pd.Timedelta(days=1)
    registry = tmp_path / "registry.jsonl"
    monkeypatch.setattr(run_replay, "REGISTRY", registry)
    return {"data": data_dir, "start": start.isoformat(), "cache": tmp_path / "cache", "registry": registry,
            "tmp": tmp_path}


def _argv(env, out: Path, *extra: str) -> list[str]:
    return ["--data-dir", str(env["data"]), "--symbols", *FIXTURE_SYMBOLS, "--start", env["start"],
            "--holdout-months", "0", "--out", str(out), "--workers", "1", "--cache-dir", str(env["cache"]), *extra]


def _registry_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_run_replay_writes_gate_labels_report_and_adds_cost_stress_and_monte_carlo(replay_env):
    out = replay_env["tmp"] / "out"
    assert run_replay.main(_argv(replay_env, out)) == 0

    gate = json.loads((out / f"{STEM}_gate.json").read_text(encoding="utf-8"))
    assert gate["tool"] == "r2_authenticity_gate"
    assert gate["verdict"] in {"PASS", "FAIL"}
    assert gate["evidence_status"] == ("EVIDENCE_ELIGIBLE" if gate["verdict"] == "PASS" else "NOT_EVIDENCE")
    report = json.loads((out / f"{STEM}.json").read_text(encoding="utf-8"))
    assert gate["policy_hash"] == report["policy_hash"]
    assert gate["dataset_sha256_16"] == report["dataset_sha256_16"]
    assert [c["id"] for c in gate["checks"]][:3] == ["R1A_TRUNCATION", "R1B_FUTURE_CORRUPTION", "R1C_WARMUP_PARITY"]

    first_line = (out / f"{STEM}.md").read_text(encoding="utf-8").splitlines()[0]
    assert first_line == (PASS_LINE if gate["verdict"] == "PASS" else NOT_EVIDENCE_LINE)

    dev = report["development"]
    assert dev["trades"]["trades"] > 0  # --holdout-months 0 puts every fixture trade here
    assert [row["multiple"] for row in dev["cost_stress"]["rows"]] == [1.0, 1.5, 2.0, 3.0]
    assert dev["cost_stress"]["scale_commission"] is True
    assert dev["monte_carlo"]["tool"] == "r5_monte_carlo"
    assert {row["method"] for row in dev["monte_carlo"]["rows"]} == {"iid", "block"}
    assert report["holdout"]["trades"] == {"trades": 0}

    (row,) = _registry_rows(replay_env["registry"])
    assert row["gate_verdict"] == gate["verdict"]
    assert row["evidence_status"] == gate["evidence_status"]
    assert list((replay_env["cache"] / run_replay.R1_CACHE_KIND).glob("*.json.gz"))


def test_second_run_reuses_cached_r1_audit(replay_env, monkeypatch):
    assert run_replay.main(_argv(replay_env, replay_env["tmp"] / "first")) == 0
    first = json.loads((replay_env["tmp"] / "first" / f"{STEM}_gate.json").read_text(encoding="utf-8"))

    def _fail(*_args, **_kwargs):
        raise AssertionError("R1 bias audit ran despite a cache hit")

    monkeypatch.setattr(bias_audit, "run_bias_audit", _fail)
    assert run_replay.main(_argv(replay_env, replay_env["tmp"] / "second")) == 0
    second = json.loads((replay_env["tmp"] / "second" / f"{STEM}_gate.json").read_text(encoding="utf-8"))
    assert second["checks"] == first["checks"]
    assert second["verdict"] == first["verdict"]


def test_no_gate_skips_r1_and_labels_report_not_evidence(replay_env, monkeypatch):
    def _fail(*_args, **_kwargs):
        raise AssertionError("R1 bias audit ran with --no-gate")

    monkeypatch.setattr(bias_audit, "run_bias_audit", _fail)
    out = replay_env["tmp"] / "out"
    assert run_replay.main(_argv(replay_env, out, "--no-gate")) == 0

    gate = json.loads((out / f"{STEM}_gate.json").read_text(encoding="utf-8"))
    assert gate["verdict"] == "SKIPPED"
    assert gate["evidence_status"] == "NOT_EVIDENCE"
    assert (out / f"{STEM}.md").read_text(encoding="utf-8").splitlines()[0] == run_replay.NO_GATE_LINE
    (row,) = _registry_rows(replay_env["registry"])
    assert (row["gate_verdict"], row["evidence_status"]) == ("SKIPPED", "NOT_EVIDENCE")
