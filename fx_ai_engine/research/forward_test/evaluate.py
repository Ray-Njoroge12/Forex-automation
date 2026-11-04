"""R10 go/no-go evaluator: score recorded demo trades against sealed, pre-registered criteria.

Reads the trading database read-only (SQLite ``mode=ro``; the live engine may be
writing to it) or a copy. Metrics are not redefined here: SRS win rate, average
R, average winner R and equity drawdown come from ``validation.validate_demo``;
expectancy, its bootstrap CI, profit factor, average winner/loser and the
longest losing run come from ``backtesting.replay_report.trade_metrics`` (the
functions wave 1's research tools and the replay reports use).

Verdict order (first match wins):
  REFUSED       pre-registration check failed (criteria changed after sealing, bad seal, invalid file)
  INVALID       a freeze manifest was given and the configuration no longer matches it
  ABORT         an abort criterion triggered (each abort criterion may wait for its own min_trades)
  INCONCLUSIVE  fewer closed trades or trading days than pre-registered, or a go criterion is open
                or cannot be computed
  NO_GO         a go criterion failed
  GO            every go criterion passed; manual checks still need owner sign-off

A verdict on DRAFT criteria or a backdated seal is labelled decision_grade=false.

Usage (from fx_ai_engine/):
    ../.venv/bin/python -m research.forward_test.evaluate --criteria <criteria.json> --seal <seal.json> \
        --db <trading_state.db> [--as-of ISO] [--freeze-manifest <manifest.json> --repo-root <checkout>] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import math
import operator
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from backtesting.replay_report import trade_metrics
from research.common import ARTIFACTS, run_id, write_json
from research.forward_test import freeze, prereg
from validation.validate_demo import DB_PATH, ValidationScope, _compute_metrics, _scope_sql, _table_columns

OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq}


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _scope(scope: Mapping[str, Any]) -> ValidationScope:
    return ValidationScope(
        policy_modes=(scope["policy_mode"],) if scope.get("policy_mode") else (),
        execution_modes=(scope["execution_mode"],) if scope.get("execution_mode") else (),
        evidence_stream=scope.get("evidence_stream"),
        account_scope=scope.get("account_scope"),
    )


def _in_window(value: Any, start: datetime, as_of: datetime) -> bool:
    if not value:
        return False
    try:
        ts = prereg.parse_utc(str(value))
    except ValueError:
        return False
    return start <= ts <= as_of


def load_records(db_path: Path, scope: Mapping[str, Any], start: datetime, as_of: datetime) -> dict[str, Any]:
    """Closed trades and equity samples in [start, as_of], plus unresolved feedback conflicts (any time)."""
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"DB_MISSING {db_path}")
    vscope = _scope(scope)
    warnings: list[str] = []
    conn = _connect_ro(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        trades: list[dict[str, Any]] = []
        if "trades" in tables:
            sql, params = _scope_sql(vscope, _table_columns(conn, "trades"))
            rows = conn.execute(
                "SELECT trade_id, symbol, direction, r_multiple, profit_loss, status, close_time, reason_code "
                f"FROM trades WHERE status LIKE 'CLOSED%'{sql}", params).fetchall()
            trades = sorted((dict(r) for r in rows if _in_window(r["close_time"], start, as_of)),
                            key=lambda t: prereg.parse_utc(t["close_time"]))
        else:
            warnings.append("TABLE_MISSING trades")
        equity: list[float] = []
        if "account_metrics" in tables:
            sql, params = _scope_sql(vscope, _table_columns(conn, "account_metrics"))
            rows = conn.execute(f"SELECT timestamp, equity FROM account_metrics WHERE equity IS NOT NULL{sql}",
                                params).fetchall()
            equity = [float(r["equity"]) for r in sorted((r for r in rows if _in_window(r["timestamp"], start, as_of)),
                                                         key=lambda r: prereg.parse_utc(r["timestamp"]))]
        else:
            warnings.append("TABLE_MISSING account_metrics")
        conflicts: int | None = None
        if "execution_feedback_conflicts" in tables:
            cols = _table_columns(conn, "execution_feedback_conflicts")
            clauses, params = ["resolved_at IS NULL"], []
            for key in ("evidence_stream", "account_scope"):
                if scope.get(key) and key in cols:
                    clauses.append(f"{key} = ?")
                    params.append(scope[key])
            conflicts = int(conn.execute(
                f"SELECT COUNT(*) FROM execution_feedback_conflicts WHERE {' AND '.join(clauses)}", params).fetchone()[0])
        else:
            warnings.append("TABLE_MISSING execution_feedback_conflicts")
    finally:
        conn.close()
    return {"trades": trades, "equity": equity, "unresolved_feedback_conflicts": conflicts, "warnings": warnings}


def compute_metrics(records: Mapping[str, Any], start: datetime, as_of: datetime) -> dict[str, Any]:
    trades = list(records["trades"])
    srs = _compute_metrics(trades, list(records["equity"]))
    warnings = list(records.get("warnings", [])) + list(srs.get("warnings", []))
    rows = [{"r_multiple": float(t["r_multiple"]), "net_usd": float(t.get("profit_loss") or 0.0), "partial": False,
             "break_even": False, "exit_reason": t.get("reason_code") or t.get("status")}
            for t in trades if t.get("r_multiple") is not None]
    if len(rows) < len(trades):
        warnings.append(f"{len(trades) - len(rows)} closed trades have no r_multiple and are excluded from R metrics")
    tm = trade_metrics(rows) if rows else {}
    pf = tm.get("profit_factor")
    if rows and all(abs(r["net_usd"]) < 1e-9 for r in rows):
        pf = None
        warnings.append("profit_factor not computed: every closed trade has zero profit_loss")
    elif isinstance(pf, float) and math.isinf(pf):
        warnings.append("profit_factor is infinite: no losing trade in the window")
    n = srs["total_trades"]
    days = int(np.busday_count(start.date(), as_of.date())) if as_of > start else 0
    ci = tm.get("expectancy_r_ci95") or [None, None]
    return {
        "closed_trades": n,
        "trading_days_elapsed": days,
        "win_rate": srs["win_rate"] if n else None,
        "avg_r_all": srs["avg_r"] if n else None,
        "avg_r_winners_srs": srs["avg_r_winners"] if n else None,
        "expectancy_r": tm.get("expectancy_r"),
        "expectancy_r_ci95_low": ci[0],
        "expectancy_r_ci95_high": ci[1],
        "profit_factor": pf,
        "avg_winner_r": tm.get("avg_winner_r"),
        "avg_loser_r": tm.get("avg_loser_r"),
        "max_consecutive_losses": tm.get("max_consecutive_losses"),
        "max_drawdown": srs["max_drawdown"],
        "unresolved_feedback_conflicts": records["unresolved_feedback_conflicts"],
        "equity_samples": len(records["equity"]),
        "warnings": warnings,
    }


def evaluate_criteria(criteria: Mapping[str, Any], metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    n = metrics["closed_trades"]
    for group in ("go", "abort"):
        for c in criteria[group]:
            actual = metrics.get(c["metric"])
            row = {"group": group, "id": c["id"], "metric": c["metric"], "op": c["op"], "value": c["value"],
                   "actual": actual, "status": c["status"], "source": c.get("source", "")}
            if c["value"] is None:
                row["result"] = "OPEN"
            elif group == "abort" and n < int(c.get("min_trades", 0)):
                row["result"] = "NOT_YET"
            elif actual is None:
                row["result"] = "NOT_EVALUABLE"
            else:
                hit = OPS[c["op"]](float(actual), float(c["value"]))
                row["result"] = ("TRIGGERED" if hit else "CLEAR") if group == "abort" else ("PASS" if hit else "FAIL")
            out.append(row)
    return out


def decide(criteria: Mapping[str, Any], metrics: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> tuple[str, list[str]]:
    triggered = [r["id"] for r in results if r["result"] == "TRIGGERED"]
    if triggered:
        return "ABORT", [f"ABORT_TRIGGERED {', '.join(triggered)}"]
    sample = criteria["sample"]
    short = []
    if metrics["closed_trades"] < sample["min_closed_trades"]:
        short.append(f"SAMPLE_BELOW_MINIMUM closed_trades {metrics['closed_trades']} < {sample['min_closed_trades']}")
    if metrics["trading_days_elapsed"] < sample["min_trading_days"]:
        short.append(f"WINDOW_BELOW_MINIMUM trading_days {metrics['trading_days_elapsed']} < {sample['min_trading_days']}")
    if short:
        return "INCONCLUSIVE", short
    go = [r for r in results if r["group"] == "go"]
    failed = [r["id"] for r in go if r["result"] == "FAIL"]
    if failed:
        return "NO_GO", [f"GO_CRITERIA_FAILED {', '.join(failed)}"]
    pending = [f"{r['id']}={r['result']}" for r in go if r["result"] in {"OPEN", "NOT_EVALUABLE"}]
    if pending:
        return "INCONCLUSIVE", [f"GO_CRITERIA_UNDECIDED {', '.join(pending)}"]
    return "GO", ["GO_CRITERIA_MET", f"MANUAL_CHECKS_PENDING {len(criteria.get('manual_checks', []))}"]


def run(criteria_path: Path, seal_path: Path, db_path: Path, *, as_of: datetime | None = None,
        freeze_manifest: Path | None = None, repo_root: Path | None = None,
        freeze_kwargs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    as_of = prereg.parse_utc(as_of or datetime.now(timezone.utc))
    seal_record = json.loads(Path(seal_path).read_text(encoding="utf-8"))
    check = prereg.verify(seal_record, Path(criteria_path))
    report: dict[str, Any] = {
        "tool": "r10_forward_evaluate", "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "as_of_utc": as_of.isoformat(), "criteria_path": str(criteria_path), "seal_path": str(seal_path),
        "seal_sha256": seal_record.get("seal_sha256"), "db_path": str(db_path),
        "preregistration": {k: check.get(k) for k in ("ok", "reasons", "warnings", "start_utc", "criteria_sha256",
                                                     "sealed_sha256", "criteria_mtime_utc")},
    }
    if not check["ok"]:
        return {**report, "verdict": "REFUSED", "reasons": list(check["reasons"]), "decision_grade": False}
    criteria = check["criteria"]
    start = prereg.parse_utc(seal_record["start_utc"])
    report.update(criteria_id=criteria["criteria_id"], criteria_version=criteria["criteria_version"],
                  criteria_status=criteria["status"], scope=criteria["scope"], sample=criteria["sample"],
                  window={"start_utc": start.isoformat(), "end_utc": as_of.isoformat()},
                  manual_checks=criteria.get("manual_checks", []))
    decision_grade = criteria["status"] == "APPROVED" and not seal_record.get("backdated")
    report["not_decision_grade_because"] = (
        ([f"criteria status {criteria['status']}"] if criteria["status"] != "APPROVED" else [])
        + (["seal is backdated (not pre-registered)"] if seal_record.get("backdated") else []))
    if freeze_manifest is not None:
        manifest = json.loads(Path(freeze_manifest).read_text(encoding="utf-8"))
        fc = freeze.check_manifest(manifest, Path(repo_root or freeze.DEFAULT_REPO_ROOT), **dict(freeze_kwargs or {}))
        mismatches, fverdict = list(fc["mismatches"]), fc["verdict"]
        pinned = criteria.get("freeze_manifest_sha256")
        if pinned and pinned != fc["manifest_sha256"]:  # criteria name the manifest they were written for
            mismatches.append({"code": "FREEZE_MANIFEST_NOT_PREREGISTERED", "item": "freeze_manifest_sha256",
                               "expected": pinned, "actual": fc["manifest_sha256"]})
            fverdict = "MISMATCH" if fverdict == "MATCH" else fverdict
        report["freeze"] = {"verdict": fverdict, "manifest_sha256": fc["manifest_sha256"], "mismatches": mismatches}
        if fverdict != "MATCH":
            codes = sorted({m["code"] for m in mismatches})
            return {**report, "verdict": "INVALID", "reasons": [f"FREEZE_{fverdict} {', '.join(codes)}"],
                    "decision_grade": False}
    else:
        report["freeze"] = None
    metrics = compute_metrics(load_records(Path(db_path), criteria["scope"], start, as_of), start, as_of)
    results = evaluate_criteria(criteria, metrics)
    verdict, reasons = decide(criteria, metrics, results)
    if report["freeze"] is None:
        metrics["warnings"].append("FREEZE_NOT_CHECKED: no freeze manifest given")
    return {**report, "metrics": metrics, "results": results, "verdict": verdict, "reasons": reasons,
            "decision_grade": decision_grade}


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = ["# Forward-test go/no-go evaluation (R10)", ""]
    if not report.get("decision_grade"):
        why = list(report.get("not_decision_grade_because") or [])
        if report["verdict"] in {"REFUSED", "INVALID"}:
            why.append(f"run {report['verdict']}")
        lines += [f"**NOT A DECISION: {'; '.join(why)}.**", ""]
    lines += [f"Verdict: **{report['verdict']}** ({'; '.join(report['reasons'])})", "",
              f"- Criteria: `{report['criteria_path']}`"
              + (f" ({report['criteria_id']} v{report['criteria_version']}, {report['criteria_status']})"
                 if "criteria_id" in report else ""),
              f"- Seal: `{report['seal_path']}` ({str(report.get('seal_sha256'))[:16]}); "
              f"pre-registration {'OK' if report['preregistration']['ok'] else 'REFUSED'} "
              f"{' '.join(report['preregistration']['reasons'])}".rstrip(),
              f"- Database (read-only): `{report['db_path']}`"]
    for w in report["preregistration"]["warnings"]:
        lines.append(f"- warning: {w}")
    if report.get("freeze"):
        f = report["freeze"]
        lines.append(f"- Freeze: {f['verdict']} ({', '.join(sorted({m['code'] for m in f['mismatches']})) or 'no mismatches'})")
    if "window" in report:
        lines.append(f"- Window: {report['window']['start_utc']} to {report['window']['end_utc']}; "
                     f"minimum {report['sample']['min_closed_trades']} closed trades and "
                     f"{report['sample']['min_trading_days']} trading days")
    if "metrics" in report:
        m = report["metrics"]
        lines += ["", "| metric | value |", "| --- | --- |"]
        lines += [f"| {k} | {v} |" for k, v in m.items() if k != "warnings"]
        lines += ["", "| group | id | metric | rule | actual | result | criterion status |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in report["results"]:
            rule = f"{r['op']} {r['value']}" if r["value"] is not None else "open"
            lines.append(f"| {r['group']} | {r['id']} | {r['metric']} | {rule} | {r['actual']} | {r['result']} | {r['status']} |")
        for w in m["warnings"]:
            lines.append(f"\n- data warning: {w}")
    if report.get("manual_checks"):
        lines += ["", "Manual checks (owner sign-off, not automated):"]
        lines += [f"- {c['id']}: {c['text']}" for c in report["manual_checks"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R10 go/no-go evaluator over recorded demo trades (read-only)")
    parser.add_argument("--criteria", type=Path, required=True)
    parser.add_argument("--seal", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DB_PATH, help=f"trading database, opened mode=ro (default {DB_PATH})")
    parser.add_argument("--as-of", default=None, help="window end, ISO 8601 UTC (default now)")
    parser.add_argument("--freeze-manifest", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=None, help="checkout to check against the freeze manifest")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        report = run(args.criteria, args.seal, args.db, as_of=prereg.parse_utc(args.as_of) if args.as_of else None,
                     freeze_manifest=args.freeze_manifest, repo_root=args.repo_root)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2
    out = Path(args.out or ARTIFACTS / "forward_test" / run_id())
    write_json(out / "evaluation.json", report)
    md = render_markdown(report)
    (out / "evaluation.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"Outputs: {out}")
    return 0 if report["verdict"] == "GO" else 2 if report["verdict"] == "REFUSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
