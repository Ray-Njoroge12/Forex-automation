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
    assert report["n_live"] == 35 and report["verdict"] in ("WITHIN_BANDS", "INVESTIGATE")
    assert report["inputs"]["closed_trades"] == 36 and report["inputs"]["without_net_r"] == {"no closing deal": 1}
    assert report["replay_gate"]["evidence_status"] == "NOT_EVIDENCE"
    assert report["inputs"]["replay_trade_count"] == 200
    assert "account_scope" not in json.dumps(report)
    md = (out / "drift_monitor.md").read_text(encoding="utf-8")
    assert md.startswith("# Live vs replay drift (R8)") and SHA in md and "NOT EVIDENCE" in md
    assert "| ... |" in md
    assert len(pd.read_csv(out / "bands.csv")) == 35


def test_live_r_is_net_of_commission(tmp_path):
    fixture = tmp_path / "fixture"
    build_fixture(fixture, seed=3)
    ids, live_r, meta = dm.load_live(fixture / "trading_state.db", dr.DEFAULT_STREAM, fixture / "exits",
                                     fixture / "deals.json")
    conn = dr.open_ro(fixture / "trading_state.db")
    deals, _ = dr.load_deals(fixture / "deals.json")
    closed = {t["trade_id"]: t for t in dr.closed_trades(conn, deals=deals)}
    conn.close()
    assert len(ids) == 35  # SYN_MISSING has no closing deal, so no net R
    assert meta["without_net_r"] == {"no closing deal": 1}
    assert live_r == [closed[i]["net_r"] for i in ids]
    # the fixture charges 7 USD per lot round trip; the EA-style r_multiple leaves it out
    assert meta["mean_ea_r_minus_net_r"] > 0.02
    assert all(closed[i]["r_multiple"] > closed[i]["net_r"] for i in ids)


def _gate(tmp_path, **fields):
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps({"tool": "r2_authenticity_gate", "verdict": "PASS",
                                "evidence_status": "EVIDENCE_ELIGIBLE", **fields}), encoding="utf-8")
    return path


def test_gate_verdict_is_checked_against_the_replay_csv(tmp_path):
    csv = tmp_path / "replay.csv"
    pd.DataFrame({"trade_id": ["a", "b", "c"], "exit_time": ["2025-01-06T00:00:00Z"] * 3,
                  "r_multiple": [2.2, -1.0, -1.0]}).to_csv(csv, index=False)
    same_count = dm.load_gate(_gate(tmp_path, trades=3), csv)
    assert same_count["binding"] == "UNVERIFIED" and same_count["evidence_status"] == "EVIDENCE_ELIGIBLE"
    other = dm.load_gate(_gate(tmp_path, trades=19), csv)
    assert other["binding"] == "MISMATCH" and other["evidence_status"] == "NOT_EVIDENCE"
    bound = dm.load_gate(_gate(tmp_path, trades=3, replay_trades_sha256_16=dm._sha16(csv)), csv)
    assert bound["binding"] == "BOUND" and bound["evidence_status"] == "EVIDENCE_ELIGIBLE"
    wrong_hash = dm.load_gate(_gate(tmp_path, trades=3, replay_trades_sha256_16="0" * 16), csv)
    assert wrong_hash["binding"] == "MISMATCH" and wrong_hash["evidence_status"] == "NOT_EVIDENCE"
    assert dm.load_gate(None, csv)["evidence_status"] == "NOT_EVIDENCE"


def test_main_with_mismatched_gate_reports_not_evidence(tmp_path):
    fixture = tmp_path / "fixture"
    build_fixture(fixture, seed=3)
    out = tmp_path / "r8"
    gate = _gate(tmp_path, trades=19)
    assert dm.main(["--db", str(fixture / "trading_state.db"), "--replay-trades-csv", str(fixture / "replay_trades.csv"),
                    "--deals", str(fixture / "deals.json"), "--gate-verdict", str(gate), "--out", str(out)]) == 0
    report = json.loads((out / "drift_monitor.json").read_text(encoding="utf-8"))
    assert report["replay_gate"]["binding"] == "MISMATCH"
    assert report["replay_gate"]["evidence_status"] == "NOT_EVIDENCE"
    assert report["r_definition"] == dr.R_DEFINITION
    md = (out / "drift_monitor.md").read_text(encoding="utf-8")
    assert "Gate binding: MISMATCH" in md and "net R" in md
