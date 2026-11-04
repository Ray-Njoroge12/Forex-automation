"""R8 drift monitor: thresholds from the versioned config, bands, flags, fail-closed small samples, reproducibility."""
from __future__ import annotations
import json
import numpy as np
import pandas as pd
import pytest
from research import demo_records as dr
from research import drift_monitor as dm
from research.monte_carlo import PERCENTILES
from research.synthetic_demo import build_fixture
CFG, VERSION, SHA = dm.load_config()
REPLAY = np.where(np.random.default_rng(5).random(200) < 0.35, 2.2, -1.0).tolist()


def test_cfg_is_read_from_thresholds_file(tmp_path, monkeypatch):
    raw = json.loads(dr.DEFAULT_THRESHOLDS.read_text(encoding="utf-8"))
    assert CFG == raw["r8_drift_monitor"]
    assert VERSION == raw["version"]
    live = [2.2, -1.0, 2.2]
    assert dm.drift_report(REPLAY, live, CFG)["verdict"] == "INCONCLUSIVE"
    raw["r8_drift_monitor"]["min_live_trades"] = 3
    copy = tmp_path / "thresholds.json"
    copy.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(dr, "DEFAULT_THRESHOLDS", copy)
    cfg, version, sha = dm.load_config()
    assert cfg["min_live_trades"] == 3 and sha != SHA
    assert dm.drift_report(REPLAY, live, cfg)["verdict"] in ("WITHIN_BANDS", "INVESTIGATE")


def test_live_drawn_from_replay_is_within_bands():
    live = np.random.default_rng(2).choice(REPLAY, 40).tolist()
    report = dm.drift_report(REPLAY, live, CFG)
    assert report["n_live"] == 40
    assert report["verdict"] == "WITHIN_BANDS"
    assert report["flags"] == []


def test_all_losses_fire_investigate():
    report = dm.drift_report(REPLAY, [-1.0] * 30, CFG)
    assert report["verdict"] == "INVESTIGATE"
    assert "CUM_R_BELOW_BAND" in report["flags"]
    assert "LOSS_STREAK_ABOVE_BAND" in report["flags"]
    assert "CUM_R_ABOVE_BAND" not in report["flags"]
    assert report["metrics"]["cum_r"]["live"] == pytest.approx(-30.0)
    assert report["metrics"]["loss_streak"]["live"] == 30


def test_small_and_empty_samples_are_inconclusive():
    n = CFG["min_live_trades"] - 1
    small = dm.drift_report(REPLAY, [-1.0] * n, CFG)
    assert small["verdict"] == "INCONCLUSIVE"
    assert small["flags"] and small["flags_informational"] is True
    empty = dm.drift_report(REPLAY, [], CFG)
    assert empty["verdict"] == "INCONCLUSIVE"
    assert empty["reason"] == "NO_CLOSED_LIVE_TRADES"
    assert empty["bands"] == []
    with pytest.raises(ValueError):
        dm.drift_report([], [1.0], CFG)


def test_band_percentiles_ordered_and_rank_bounded():
    live = np.random.default_rng(4).choice(REPLAY, 30).tolist()
    report = dm.drift_report(REPLAY, live, CFG, live_ids=[f"T{i}" for i in range(30)])
    assert [b["k"] for b in report["bands"]] == list(range(1, 31))
    assert report["bands"][0]["trade_id"] == "T0"
    for band in report["bands"]:
        values = [band[f"p{p}"] for p in PERCENTILES]
        assert values == sorted(values)
        assert 0.0 <= band["live_rank"] <= 100.0
    assert report["bands"][-1]["live_cum_r"] == pytest.approx(report["metrics"]["cum_r"]["live"])


def test_same_seed_gives_identical_json():
    cfg = {**CFG, "runs": 2000}
    live = np.random.default_rng(9).choice(REPLAY, 26).tolist()
    a = json.dumps(dm.drift_report(REPLAY, live, cfg), sort_keys=True)
    b = json.dumps(dm.drift_report(REPLAY, live, cfg), sort_keys=True)
    assert a == b


def test_drawdown_and_loss_streak_helpers():
    assert dm.max_drawdown_r([1, -2, 0.5, -1]) == pytest.approx(2.5)
    assert dm.max_drawdown_r([-1.0]) == pytest.approx(1.0)
    assert dm.max_drawdown_r([1.0, 2.0]) == pytest.approx(0.0)
    assert dm.longest_loss_streak([-1, -1, 2, -1]) == 2
    assert dm.longest_loss_streak([2.2, 0.0]) == 0


def test_main_on_fixture_writes_outputs(tmp_path):
    fixture = tmp_path / "fixture"
    build_fixture(fixture, seed=3)
    out = tmp_path / "r8"
    rc = dm.main(["--db", str(fixture / "trading_state.db"), "--replay-trades-csv", str(fixture / "replay_trades.csv"),
                  "--exits-dir", str(fixture / "exits"), "--deals", str(fixture / "deals.json"), "--out", str(out)])
    assert rc == 0
    for name in ("drift_monitor.json", "drift_monitor.md", "bands.csv"):
        assert (out / name).is_file()
    report = json.loads((out / "drift_monitor.json").read_text(encoding="utf-8"))
    assert report["thresholds_version"] == VERSION and report["thresholds_sha256_16"] == SHA
    assert report["n_live"] == 36 and report["verdict"] in ("WITHIN_BANDS", "INVESTIGATE")
    assert report["replay_gate"]["evidence_status"] == "NOT_EVIDENCE"
    assert report["inputs"]["replay_trade_count"] == 200
    assert "account_scope" not in json.dumps(report)
    md = (out / "drift_monitor.md").read_text(encoding="utf-8")
    assert md.startswith("# Live vs replay drift (R8)") and SHA in md and "NOT EVIDENCE" in md
    assert "| ... |" in md
    assert len(pd.read_csv(out / "bands.csv")) == 36
