"""R10 go/no-go evaluator on the synthetic fixture DB: verdicts, refusal, read-only access, metric reuse."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtesting.replay_report import trade_metrics
from research.forward_test import evaluate, freeze, prereg

FIXTURE_SQL = Path(__file__).parent / "fixtures" / "forward_test" / "demo_trades.sql"
DRAFT = Path(__file__).parents[1] / "docs" / "research" / "forward-test" / "criteria-srs-v1.1-draft.json"
START = datetime(2027, 1, 4, tzinfo=timezone.utc)
AS_OF = datetime(2027, 2, 15, tzinfo=timezone.utc)
BEFORE = START - timedelta(days=3)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "trading_state.db"
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE_SQL.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    return path


def _sealed(tmp_path, edit=None, name="criteria.json"):
    path = tmp_path / name
    c = json.loads(DRAFT.read_text(encoding="utf-8"))
    if edit:
        edit(c)
    path.write_text(json.dumps(c, indent=2), encoding="utf-8")
    os.utime(path, (BEFORE.timestamp(), BEFORE.timestamp()))
    seal_path = tmp_path / (name + ".seal.json")
    seal_path.write_text(json.dumps(prereg.seal(path, START, now=BEFORE)), encoding="utf-8")
    return path, seal_path


def _set(group, cid, **kw):
    def edit(c):
        next(x for x in c[group] if x["id"] == cid).update(kw)
    return edit


def test_draft_criteria_on_fixture_is_no_go_on_win_rate(tmp_path, db):
    crit, seal = _sealed(tmp_path)
    report = evaluate.run(crit, seal, db, as_of=AS_OF)
    m = report["metrics"]
    assert (m["closed_trades"], m["trading_days_elapsed"], m["unresolved_feedback_conflicts"]) == (30, 30, 0)
    assert m["win_rate"] == pytest.approx(13 / 30, abs=1e-4)
    assert m["expectancy_r"] == pytest.approx(0.3)
    assert m["profit_factor"] == pytest.approx(832 / 544, abs=1e-4)
    assert m["avg_winner_r"] == 2.0 and m["max_drawdown"] == pytest.approx(0.0602, abs=1e-4)
    assert report["verdict"] == "NO_GO" and report["reasons"] == ["GO_CRITERIA_FAILED G4_WIN_RATE"]
    assert report["decision_grade"] is False  # draft criteria
    results = {r["id"]: r["result"] for r in report["results"]}
    assert results["A3_AVG_R_REPLACEMENT"] == "OPEN" and results["A1_DRAWDOWN"] == "CLEAR"
    assert "NOT A DECISION" in evaluate.render_markdown(report)


def test_metrics_reuse_existing_definitions(tmp_path, db):
    crit, seal = _sealed(tmp_path)
    records = evaluate.load_records(db, {"policy_mode": "core_srs", "execution_mode": "mt5"}, START, AS_OF)
    rows = [{"r_multiple": t["r_multiple"], "net_usd": t["profit_loss"], "partial": False, "break_even": False,
             "exit_reason": t["reason_code"]} for t in records["trades"]]
    ref = trade_metrics(rows)
    m = evaluate.compute_metrics(records, START, AS_OF)
    assert m["expectancy_r_ci95_low"] == ref["expectancy_r_ci95"][0]
    assert m["max_consecutive_losses"] == ref["max_consecutive_losses"]
    assert [t["trade_id"] for t in records["trades"]][:2] == ["FX_001", "FX_002"]  # out-of-window/scope rows excluded


def test_sample_below_minimum_is_inconclusive(tmp_path, db):
    crit, seal = _sealed(tmp_path)
    report = evaluate.run(crit, seal, db, as_of=datetime(2027, 1, 20, tzinfo=timezone.utc))
    assert report["metrics"]["closed_trades"] == 12
    assert report["verdict"] == "INCONCLUSIVE"
    assert report["reasons"][0] == "SAMPLE_BELOW_MINIMUM closed_trades 12 < 25"


def test_evaluator_refuses_criteria_modified_after_start(tmp_path, db):
    crit, seal = _sealed(tmp_path)
    crit.write_text(crit.read_text(encoding="utf-8").replace('"value": 0.45', '"value": 0.40'), encoding="utf-8")
    later = (START + timedelta(days=10)).timestamp()
    os.utime(crit, (later, later))
    report = evaluate.run(crit, seal, db, as_of=AS_OF)
    assert report["verdict"] == "REFUSED"
    assert report["reasons"] == ["CRITERIA_MODIFIED_AFTER_START"]
    assert "metrics" not in report
    assert evaluate.main(["--criteria", str(crit), "--seal", str(seal), "--db", str(db),
                          "--as-of", AS_OF.isoformat(), "--out", str(tmp_path / "out")]) == 2


def test_abort_criterion_triggers_before_sample_minimum(tmp_path, db):
    crit, seal = _sealed(tmp_path, _set("abort", "A1_DRAWDOWN", value=0.05))
    report = evaluate.run(crit, seal, db, as_of=datetime(2027, 1, 20, tzinfo=timezone.utc))
    assert report["verdict"] == "ABORT" and report["reasons"] == ["ABORT_TRIGGERED A1_DRAWDOWN"]


def test_all_go_criteria_met_is_go_with_manual_checks_pending(tmp_path, db):
    crit, seal = _sealed(tmp_path, _set("go", "G4_WIN_RATE", value=0.40))
    report = evaluate.run(crit, seal, db, as_of=AS_OF)
    assert report["verdict"] == "GO"
    assert report["reasons"] == ["GO_CRITERIA_MET", "MANUAL_CHECKS_PENDING 7"]


def test_open_go_criterion_blocks_go(tmp_path, db):
    def edit(c):
        _set("go", "G4_WIN_RATE", value=0.40)(c)
        _set("go", "G3_AVG_WINNER", value=None, status="OPEN_OWNER_DECISION")(c)
    crit, seal = _sealed(tmp_path, edit)
    report = evaluate.run(crit, seal, db, as_of=AS_OF)
    assert report["verdict"] == "INCONCLUSIVE" and report["reasons"] == ["GO_CRITERIA_UNDECIDED G3_AVG_WINNER=OPEN"]


def test_unresolved_conflict_fails_reconciliation_gate(tmp_path, db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO execution_feedback_conflicts (evidence_stream, account_scope, dedupe_key, trade_id, reason, "
                 "payload_json, created_at, resolved_at) VALUES ('s','a','k','t','r','{}','2027-01-10T00:00:00+00:00',NULL)")
    conn.commit()
    conn.close()
    crit, seal = _sealed(tmp_path, _set("go", "G4_WIN_RATE", value=0.40))
    report = evaluate.run(crit, seal, db, as_of=AS_OF)
    assert report["verdict"] == "NO_GO" and report["reasons"] == ["GO_CRITERIA_FAILED G6_RECONCILIATION"]


def test_database_is_opened_read_only(tmp_path, db):
    before = freeze.sha256_file(db)
    crit, seal = _sealed(tmp_path)
    evaluate.run(crit, seal, db, as_of=AS_OF)
    assert freeze.sha256_file(db) == before
    conn = evaluate._connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM trades")
    conn.close()


def test_freeze_mismatch_invalidates_the_run(tmp_path, db):
    repo = tmp_path / "repo"
    for rel, text in {"SRS_v1.md": "x", "fx_ai_engine/config_microcapital.py": "A = 1\n",
                      "fx_ai_engine/mt5_ea/FX_Execution.mq5": 'input string ExpectedPolicyHash = "aa";\n'}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    fns = {"policy_hash_fn": lambda r: "aa", "git_fn": lambda r: None}
    manifest = tmp_path / "freeze.json"
    manifest.write_text(json.dumps(freeze.create_manifest(repo, ea_build=None, **fns)), encoding="utf-8")
    crit, seal = _sealed(tmp_path)
    ok = evaluate.run(crit, seal, db, as_of=AS_OF, freeze_manifest=manifest, repo_root=repo, freeze_kwargs=fns)
    assert ok["freeze"]["verdict"] == "MATCH" and ok["verdict"] == "NO_GO"
    (repo / "fx_ai_engine/config_microcapital.py").write_text("A = 2\n", encoding="utf-8")
    bad = evaluate.run(crit, seal, db, as_of=AS_OF, freeze_manifest=manifest, repo_root=repo, freeze_kwargs=fns)
    assert bad["verdict"] == "INVALID"
    assert bad["reasons"] == ["FREEZE_MISMATCH CONFIG_HASH_CHANGED, PROTECTED_FILE_CHANGED"]


def test_cli_writes_report(tmp_path, db, capsys):
    crit, seal = _sealed(tmp_path)
    out = tmp_path / "out"
    code = evaluate.main(["--criteria", str(crit), "--seal", str(seal), "--db", str(db),
                          "--as-of", AS_OF.isoformat(), "--out", str(out)])
    assert code == 1
    assert json.loads((out / "evaluation.json").read_text(encoding="utf-8"))["verdict"] == "NO_GO"
    assert "Verdict: **NO_GO**" in capsys.readouterr().out
