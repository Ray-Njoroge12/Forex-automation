"""R3 multiple-testing check: minimum sample, PSR/DSR, CSCV PBO, FX weeks, registry integrity and CLI."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from research import multiple_testing as mt
from research.multiple_testing_fixture import write_synthetic_fixture

EDGE = "pullback_2__fixed_tp__edge"


def _row(verdict: dict, label: str) -> dict:
    return next(v for v in verdict["variants"] if v["label"] == label)


def _fixture(tmp_path: Path, **kwargs) -> tuple[Path, Path]:
    root = tmp_path / "fx"
    return write_synthetic_fixture(root, **kwargs), root


def _csv(root: Path, label: str) -> Path:
    return root / "artifacts" / "replay" / "synthetic" / f"{label}__base_trades.csv"


# ----------------------------------------------------------------------
# Statistics core
# ----------------------------------------------------------------------

def test_min_sample_design_matches_adr_numbers():
    d = mt.min_sample_design(0.25, 2.2, 0.05, 0.80)
    assert d["q"] == pytest.approx(0.390625)
    assert d["sigma"] == pytest.approx(1.5612, abs=1e-4)
    assert d["n_exact"] == pytest.approx(306.1, abs=0.1)
    assert d["n_min"] == 307
    assert d["n_ci_only"] == 150
    assert mt.MIN_TRADES == 307


def test_expected_max_sharpe():
    assert mt.expected_max_sharpe(0.7, 1) == 0
    g = np.euler_gamma
    closed = (1 - g) * norm.ppf(1 - 1 / 10) + g * norm.ppf(1 - 1 / (10 * math.e))
    assert mt.expected_max_sharpe(1.0, 10) == pytest.approx(closed)
    values = [mt.expected_max_sharpe(0.04, n) for n in (2, 5, 18, 100)]
    assert values == sorted(values) and values[0] < values[-1]


def test_psr_closed_form_and_undefined():
    sr, n, skew, kurt = 0.1, 500, -0.5, 4.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    assert mt.psr(sr, 0.0, n, skew, kurt) == pytest.approx(norm.cdf(sr * math.sqrt(n - 1) / math.sqrt(denom)))
    assert mt.psr(1.0, 0.0, 100, 3.0, 1.0) is None  # 1 - 3 + 0 < 0
    assert mt.psr(None, 0.0, 100, 0.0, 3.0) is None


def test_sharpe_stats_constant_series_is_undefined():
    st = mt.sharpe_stats([1.0] * 10)
    assert st["sr"] is None and st["n"] == 10
    st = mt.sharpe_stats([2.2, -1.0, -1.0, 2.2, -1.0])
    assert st["sr"] == pytest.approx(np.mean([2.2, -1, -1, 2.2, -1]) / np.std([2.2, -1, -1, 2.2, -1], ddof=1))
    assert st["kurtosis"] > 0


def test_pbo_near_half_on_noise():
    rng = np.random.default_rng(11)
    res = mt.cscv_pbo(rng.standard_normal((200, 10)))
    assert res["status"] == "OK" and res["splits"] == 12870
    assert 0.3 <= res["pbo"] <= 0.7


def test_pbo_low_with_one_real_edge():
    rng = np.random.default_rng(12)
    m = rng.standard_normal((200, 10))
    m[:, 3] += 1.0
    assert mt.cscv_pbo(m)["pbo"] < 0.05


def test_pbo_unavailable_cases():
    rng = np.random.default_rng(13)
    assert mt.cscv_pbo(rng.standard_normal((200, 1)))["reason"] == "GROUP_TOO_FEW_SERIES"
    res = mt.cscv_pbo(rng.standard_normal((50, 5)))
    assert res["reason"] == "CSCV_TOO_SHORT" and res["pbo"] is None


def test_fx_week_boundaries():
    weeks = mt.fx_week(["2026-01-11T22:00:00Z", "2026-01-09T20:00:00Z", "2026-01-11T21:00:00Z"])
    # Sunday 22:00 UTC is 17:00 New York (EST): the next FX week; Friday 20:00 UTC stays in the current one.
    assert weeks[0] == pd.Period("2026-01-12", freq="W-SUN")
    assert weeks[1] == pd.Period("2026-01-05", freq="W-SUN")
    assert weeks[2] == weeks[1]


def test_weekly_matrix_fills_empty_weeks_with_zero():
    m = mt.weekly_matrix({"a": (["2026-01-06T10:00:00Z", "2026-01-07T10:00:00Z"], [1.0, -1.0]),
                          "b": (["2026-01-20T10:00:00Z"], [2.2])}, "2026-01-05", "2026-01-25T22:00:00Z")
    assert len(m) == 3
    assert m["a"].tolist() == [0.0, 0.0, 0.0] and m["b"].tolist() == [0.0, 0.0, 2.2]


# ----------------------------------------------------------------------
# Registry, integrity and verdict (synthetic fixture)
# ----------------------------------------------------------------------

def test_fixture_passes_only_the_edge(tmp_path):
    registry, root = _fixture(tmp_path)
    v = mt.evaluate(registry, root)
    assert v["verdict"] == "PASS" and v["evidence_status"] == "EVIDENCE_ELIGIBLE"
    assert v["trials_n"] == 6
    edge = _row(v, EDGE)
    assert edge["verdict"] == "PASS" and edge["evidence_status"] == "EVIDENCE_ELIGIBLE"
    others = [x for x in v["variants"] if x["label"] != EDGE]
    assert len(others) == 5 and all(x["verdict"] == "FAIL" for x in others)
    assert v["groups"][0]["cscv"]["status"] == "OK"


def test_truncated_trades_file_is_count_mismatch(tmp_path):
    registry, root = _fixture(tmp_path)
    path = _csv(root, EDGE)
    df = pd.read_csv(path)
    df.iloc[10:].to_csv(path, index=False)
    v = mt.evaluate(registry, root)
    assert _row(v, EDGE)["reasons"] == ["SERIES_COUNT_MISMATCH"]
    assert v["verdict"] != "PASS"


def test_other_run_json_is_run_mismatch(tmp_path):
    registry, root = _fixture(tmp_path)
    path = _csv(root, EDGE).with_name(f"{EDGE}__base.json")
    rep = json.loads(path.read_text())
    rep["generated_at"] = "20260702T000000Z"
    path.write_text(json.dumps(rep))
    v = mt.evaluate(registry, root)
    assert _row(v, EDGE)["reasons"] == ["SERIES_RUN_MISMATCH"]
    assert v["verdict"] == "INCONCLUSIVE"


def test_missing_noise_series_blocks_the_edge(tmp_path):
    registry, root = _fixture(tmp_path)
    _csv(root, "baseline__policy__edge").unlink()
    v = mt.evaluate(registry, root)
    assert _row(v, "baseline__policy__edge")["reasons"] == ["SERIES_FILES_MISSING"]
    edge = _row(v, EDGE)
    assert edge["verdict"] == "INCONCLUSIVE" and edge["reasons"] == ["GROUP_SERIES_INCOMPLETE"]
    assert v["verdict"] == "INCONCLUSIVE"
    assert v["missing_inputs"][0]["reason"] == "SERIES_FILES_MISSING"


def test_small_sample_cannot_pass(tmp_path):
    registry, root = _fixture(tmp_path, trades_per_config=200)
    v = mt.evaluate(registry, root)
    edge = _row(v, EDGE)
    assert edge["dev_trades"] == 200 and edge["reasons"] == ["BELOW_MIN_SAMPLE"]
    assert v["verdict"] == "INCONCLUSIVE"


def test_superseded_row_is_listed_not_evaluated(tmp_path):
    registry, root = _fixture(tmp_path)
    lines = registry.read_text().splitlines()
    old = json.loads(next(line for line in lines if EDGE in line))
    old.update(run_id="20260601T000000Z", superseded=True)
    registry.write_text(json.dumps(old) + "\n" + "\n".join(lines) + "\n")
    v = mt.evaluate(registry, root)
    assert v["rows_read"] == 7 and v["rows_evaluated"] == 6 and v["trials_n"] == 6
    assert _row(v, EDGE)["run_id"] == "20260701T000000Z" and _row(v, EDGE)["verdict"] == "PASS"
    assert v["superseded_rows"] == [{"label": EDGE, "cost": "base", "run_id": "20260601T000000Z",
                                     "registry_line": 1, "superseded_flag": True}]


def test_partial_registry_line_is_inconclusive(tmp_path):
    registry, root = _fixture(tmp_path)
    with registry.open("a", encoding="utf-8") as f:
        f.write('{"run_id": "20260701T000000Z", "variant": "baseline__hyb')
    v = mt.evaluate(registry, root)
    assert v["unparseable_lines"] == 1
    assert v["verdict"] == "INCONCLUSIVE" and v["evidence_status"] == "NOT_EVIDENCE"
    assert any(r.startswith("REGISTRY_LINE_UNPARSEABLE") for r in v["reasons"])


def test_empty_registry_is_inconclusive(tmp_path):
    registry = tmp_path / "artifacts" / "experiments" / "registry.jsonl"
    registry.parent.mkdir(parents=True)
    registry.write_text("")
    v = mt.evaluate(registry, tmp_path)
    assert v["verdict"] == "INCONCLUSIVE" and "REGISTRY_EMPTY" in v["reasons"]


def test_parse_label_shapes():
    assert mt.parse_label("baseline__policy") == ("baseline", "policy", "")
    assert mt.parse_label("pullback_2__fixed_tp__edge") == ("pullback_2", "fixed_tp", "edge")
    assert mt.parse_label("odd_label") == ("odd_label", "odd_label", "")
    rows = [{"variant": "a__policy__srs", "policy_hash": "h1", "cost": "base"},
            {"variant": "a__policy__edge", "policy_hash": "h1", "cost": "stress_1_5x"},
            {"variant": "a__hybrid", "policy_hash": "h1", "cost": "base"}]
    assert [(t["policy_hash"], t["exit_mode"]) for t in mt.trial_keys(rows)] == [("h1", "hybrid"), ("h1", "policy")]


def _tree_state(root: Path) -> dict[str, tuple[str, int]]:
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def _strict_load(path: Path) -> dict:
    def reject(name):
        raise ValueError(f"non-finite constant {name} in {path}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)


def test_cli_writes_outputs_and_leaves_inputs_untouched(tmp_path, capsys):
    registry, root = _fixture(tmp_path)
    before = _tree_state(root)
    out = tmp_path / "out"
    assert mt.main(["--registry", str(registry), "--out", str(out)]) == 0
    assert _tree_state(root) == before
    verdict = _strict_load(out / "verdict.json")
    assert verdict["verdict"] == "PASS" and verdict["tool"] == "r3_multiple_testing"
    report = (out / "report.md").read_text(encoding="utf-8")
    assert report.splitlines()[0] == mt.PASS_LINE
    assert "EVIDENCE_ELIGIBLE" in report and "NOT EVIDENCE" in report
    assert "R3 multiple-testing verdict: PASS" in capsys.readouterr().out

    _csv(root, "coded_defaults__policy__edge").unlink()
    out2 = tmp_path / "out2"
    assert mt.main(["--registry", str(registry), "--out", str(out2)]) == 1
    verdict2 = _strict_load(out2 / "verdict.json")
    assert verdict2["verdict"] == "INCONCLUSIVE" and verdict2["evidence_status"] == "NOT_EVIDENCE"
    assert (out2 / "report.md").read_text(encoding="utf-8").splitlines()[0] == mt.NOT_EVIDENCE_LINE
