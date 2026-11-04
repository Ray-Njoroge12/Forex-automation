"""R9: signal postmortem of closed demo trades (rules-based, deterministic, report only).

Offline and read-only. Each closed strategy trade (research.demo_records.closed_trades,
same exit_time rules) is classified by probable cause with fixed, ordered rules over
recorded data only: the decision funnel (regime at entry and during the trade,
stop pips), the economic calendar, open execution issues and the trade's R7 row
(research.fill_reconciliation.reconcile, matched by trade_id). No LLM calls, no
network, no randomness; the run timestamp appears only in the output metadata.
No halt or veto authority: nothing in the engine reads this output.

Rules (all evaluated, all flags kept; thresholds from live_vs_replay_thresholds.json
``r9_postmortem``):
  EXECUTION       entry fill vs replay fill, fill latency after bar close, exit
                  slippage beyond the model, exit deal not placed by the EA, or an
                  open feedback conflict / EXECUTION_UNCERTAIN reservation;
  NEWS_CALENDAR   high-impact event for either currency inside
                  [entry - news_before_entry_minutes, exit + news_after_exit_minutes];
  SPREAD_COST     spread + slippage + exit slippage + commission >= cost_r_fraction R,
                  or a loss that costs alone turned from a gain;
  REGIME_MISMATCH (losing trades only) borderline ADX at entry, or a different
                  regime recorded for the symbol during the trade.
Primary cause = first flag in PRECEDENCE, else ORDINARY_NOISE.

Usage (from fx_ai_engine/):
    USE_MT5_MOCK=1 ../.venv/bin/python -m research.trade_postmortem --db <db> --deals <deals.json> --bars-dir <dir> [--exits-dir <dir>] [--feedback-dir <dir>] [--calendar <json>] [--week YYYY-Www] --out <dir>
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from backtesting.unified_replay import COST_SCENARIOS
from research.common import ARTIFACTS, parse_ts, run_id, write_json
from research.demo_records import (
    DEFAULT_CALENDAR, DEFAULT_STREAM, calendar_coverage, closed_trades, guard_output_dir, load_calendar_events,
    load_deals, load_exits, load_feedback_files, load_funnel, load_m15, load_open_issues, load_thresholds, open_ro,
    parse_details,
)
from research.fill_reconciliation import reconcile

PRECEDENCE = ("EXECUTION", "NEWS_CALENDAR", "SPREAD_COST", "REGIME_MISMATCH")
DEFAULT_CAUSE = "ORDINARY_NOISE"
CAUSES = PRECEDENCE + (DEFAULT_CAUSE,)
PATTERN_SHARE = 0.25
REPORT_ONLY = ("Report only: no halt or veto authority; nothing in the engine reads this output, "
               "and trading behaviour is unchanged.")
NOTE = ("Rules-based and deterministic classification of closed demo trades by probable cause, "
        "from recorded data only (no LLM, no network). A cause is a hypothesis for a human to check.")
NOISE_MEANING = "outcome consistent with the model; nothing abnormal recorded"
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


def _r(value: float | None, nd: int = 4) -> float | None:
    return None if value is None else round(float(value), nd) + 0.0


def _sha16_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _sha16_dir(directory: str | Path, pattern: str) -> tuple[str | None, int]:
    d = Path(directory)
    files = sorted(p for p in d.rglob(pattern) if p.is_file()) if d.is_dir() else []
    if not files:
        return None, 0
    h = hashlib.sha256()
    for p in files:
        h.update(p.relative_to(d).as_posix().encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()[:16], len(files)


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _iso(ts: pd.Timestamp | None) -> str | None:
    return None if ts is None else ts.isoformat()


def iso_week(ts: pd.Timestamp | None) -> str:
    return "undated" if ts is None else ts.strftime("%G-W%V")


def week_bounds(week: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(datetime.strptime(week + "-1", "%G-W%V-%u").replace(tzinfo=timezone.utc))
    return start, start + pd.Timedelta(days=7)


# ----------------------------------------------------------------------
# Inputs per trade
# ----------------------------------------------------------------------
def _with_time(rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    out = []
    for row in rows:
        try:
            t = parse_ts(str(row["decision_time"])) if row.get("decision_time") else None
        except ValueError:
            t = None
        out.append({**row, "_t": t})
    return out


def prepare_trades(closed: Sequence[Mapping[str, Any]], r7_rows: Sequence[Mapping[str, Any]],
                   funnel: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Normalised trade records: decision/entry/exit times, net R, stop pips and the trade's R7 row."""
    r7_by_id: dict[str, dict] = {}
    for row in r7_rows:
        tid = row.get("trade_id")
        if tid is None:
            continue
        if tid not in r7_by_id or (row.get("comparable") and not r7_by_id[tid].get("comparable")):
            r7_by_id[str(tid)] = dict(row)
    routed: dict[str, pd.Timestamp] = {}
    stops: dict[str, float] = {}
    for row in funnel:
        tid = row.get("trade_id")
        if not tid:
            continue
        if row.get("stage") == "ROUTER" and row.get("outcome") == "ROUTED" and row.get("_t") is not None:
            routed.setdefault(str(tid), row["_t"])
        if row.get("stage") == "TECHNICAL" and row.get("outcome") == "PASS":
            stop = _num(parse_details(row.get("details")).get("stop_pips"))
            if stop is not None:
                stops.setdefault(str(tid), stop)
    out = []
    for trade in closed:
        tid = str(trade.get("trade_id"))
        notes: list[str] = []
        r7 = r7_by_id.get(tid)
        decision = routed.get(tid)
        if decision is None and trade.get("open_time"):
            decision = parse_ts(str(trade["open_time"]))
            notes.append("decision time from trades.open_time (no ROUTER row)")
        entry = parse_ts(r7["fill_time"]) if r7 and r7.get("fill_time") else None
        if entry is None and trade.get("execution_time"):
            entry = parse_ts(str(trade["execution_time"]))
            notes.append("entry time from trades.execution_time (no entry deal)")
        if entry is None and decision is not None:
            entry = decision
            notes.append("entry time = decision time (no deal, no execution_time)")
        stop, stop_source = stops.get(tid), "funnel TECHNICAL stop_pips"
        if stop is None:
            stop, stop_source = _num(trade.get("stop_loss")), "trades.stop_loss (fallback)"
        exit_time = trade.get("exit_time")
        if trade.get("exit_time_unverified"):
            notes.append("exit time from exit file close_time (server time, unverified)")
        out.append({
            "trade_id": tid, "symbol": str(trade.get("symbol") or ""), "direction": trade.get("direction"),
            "status": trade.get("status"), "source": trade.get("source"),
            "decision_time": decision, "entry_time": entry, "exit_time": exit_time,
            "net_r": _num(trade.get("r_multiple")), "profit_loss": _num(trade.get("profit_loss")),
            "stop_pips": stop, "stop_source": stop_source, "r7": r7, "prep_notes": notes,
        })
    return out


# ----------------------------------------------------------------------
# Rules
# ----------------------------------------------------------------------
def _flag(cause: str, evidence: str) -> dict:
    return {"cause": cause, "evidence": evidence}


def _execution_flags(trade: Mapping[str, Any], r7: Mapping[str, Any] | None, comparable: bool,
                     open_issues: Mapping[str, set], cfg: Mapping[str, Any]) -> list[dict]:
    flags = []
    if r7 is not None and comparable:
        diff = _num(r7.get("fill_diff_pips"))
        if diff is not None and diff >= cfg["execution_entry_adverse_pips"]:
            flags.append(_flag("EXECUTION", f"entry fill {diff:+.2f} pip worse than the replay fill "
                                            f"(limit {cfg['execution_entry_adverse_pips']:g})"))
        latency = _num(r7.get("fill_latency_s"))
        if latency is not None and latency >= cfg["execution_latency_seconds"]:
            flags.append(_flag("EXECUTION", f"entry deal {latency:.1f} s after bar close "
                                            f"(limit {cfg['execution_latency_seconds']:g} s)"))
        exit_diff = _num(r7.get("exit_slippage_diff_pips"))
        if exit_diff is not None and exit_diff >= cfg["execution_exit_adverse_pips"]:
            flags.append(_flag("EXECUTION", f"exit slippage {exit_diff:+.2f} pip beyond the model "
                                            f"(limit {cfg['execution_exit_adverse_pips']:g})"))
    if r7 is not None and r7.get("exit_kind") == "NOT_BY_EA":
        flags.append(_flag("EXECUTION", "exit deal not placed by the EA (magic 0, no sl/tp tag): possible manual close"))
    tid = trade["trade_id"]
    if tid in open_issues.get("conflicts", set()):
        flags.append(_flag("EXECUTION", "unresolved execution feedback conflict"))
    if tid in open_issues.get("uncertain", set()):
        flags.append(_flag("EXECUTION", "reservation in EXECUTION_UNCERTAIN"))
    return flags


def _news(trade: Mapping[str, Any], events: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]
          ) -> tuple[list[dict], list[str]]:
    entry, exit_ = trade.get("entry_time"), trade.get("exit_time")
    if entry is None or exit_ is None:
        return [], ["NEWS_NOT_EVALUABLE: entry or exit time missing"]
    lo = entry - pd.Timedelta(minutes=cfg["news_before_entry_minutes"])
    hi = exit_ + pd.Timedelta(minutes=cfg["news_after_exit_minutes"])
    sym = trade["symbol"]
    currencies = {sym[:3], sym[3:]}
    flags = []
    for e in events:
        if str(e.get("impact", "")).lower() != "high" or str(e.get("currency", "")).upper() not in currencies:
            continue
        if lo <= e["time"] <= hi:
            minutes = (e["time"] - entry).total_seconds() / 60.0
            flags.append(_flag("NEWS_CALENDAR", f"high-impact {e.get('currency')} '{e.get('event')}' at "
                                                f"{e['time'].isoformat()} ({minutes:+.0f} min from entry)"))
    notes = []
    cov_lo, cov_hi = calendar_coverage(events)
    if cov_lo is None or lo < cov_lo or hi > cov_hi:
        notes.append("NEWS_NOT_EVALUABLE: trade window "
                     f"[{lo.isoformat()}, {hi.isoformat()}] not inside calendar coverage "
                     f"[{_iso(cov_lo)}, {_iso(cov_hi)}]")
    return flags, notes


def _cost(trade: Mapping[str, Any], r7: Mapping[str, Any] | None, comparable: bool, cfg: Mapping[str, Any]
          ) -> tuple[list[dict], list[str], float | None]:
    if r7 is None or not comparable:
        return [], [], None
    spread, implied = _num(r7.get("actual_spread_pips")), _num(r7.get("implied_slippage_pips"))
    stop = trade.get("stop_pips")
    if spread is None or implied is None or not stop or stop <= 0:
        return [], ["SPREAD_COST not evaluated: actual spread, implied slippage or stop pips missing"], None
    exit_slip = _num(r7.get("exit_slippage_pips")) or 0.0
    pips = spread + max(0.0, implied) + max(0.0, exit_slip)
    cost_r = pips / stop
    notes = []
    detail = (f"spread {spread:.2f} + slippage {max(0.0, implied):.2f} + exit slippage {max(0.0, exit_slip):.2f} pip "
              f"over a {stop:g}-pip stop ({trade.get('stop_source')})")
    net_r, pl = trade.get("net_r"), trade.get("profit_loss")
    per_lot, volume = _num(r7.get("commission_per_lot_usd")), _num(r7.get("volume"))
    if net_r and pl is not None and per_lot is not None and volume:
        risk_usd = abs(pl / net_r)
        commission = per_lot * volume
        cost_r += commission / risk_usd
        detail += f" + commission {commission:.2f} USD / risk {risk_usd:.2f} USD"
    else:
        notes.append("commission term skipped (r_multiple is 0 or commission/volume missing)")
    flags = []
    if cost_r >= cfg["cost_r_fraction"]:
        flags.append(_flag("SPREAD_COST", f"costs {cost_r:.3f} R >= {cfg['cost_r_fraction']:g} R: {detail}"))
    elif net_r is not None and net_r <= 0 and net_r + cost_r > 0:
        flags.append(_flag("SPREAD_COST", f"net {net_r:+.3f} R but {net_r + cost_r:+.3f} R before costs "
                                          f"({cost_r:.3f} R): {detail}"))
    return flags, notes, _r(cost_r)


def _regime(trade: Mapping[str, Any], regime_rows: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]
            ) -> tuple[list[dict], list[str]]:
    decision = trade.get("decision_time")
    rows = [r for r in regime_rows if r.get("symbol") == trade["symbol"] and r.get("_t") is not None]
    entry_row = next((r for r in rows if decision is not None and r["_t"] == decision), None)
    if entry_row is None:
        return [], ["INSUFFICIENT_DATA: no entry REGIME funnel row"]
    net_r = trade.get("net_r")
    if net_r is None or net_r > 0:
        return [], []
    flags = []
    details = parse_details(entry_row.get("details"))
    adx, below = _num(details.get("adx")), _num(details.get("adx_transition_below"))
    if adx is not None and below is not None and adx - below < cfg["regime_adx_margin"]:
        flags.append(_flag("REGIME_MISMATCH", f"borderline trend at entry: adx {adx:.2f} vs transition "
                                              f"{below:.2f} (margin {cfg['regime_adx_margin']:g})"))
    entry_regime = details.get("regime")
    entry, exit_ = trade.get("entry_time"), trade.get("exit_time")
    if entry is not None and exit_ is not None:
        changed = [r for r in rows if entry < r["_t"] <= exit_
                   and parse_details(r.get("details")).get("regime") != entry_regime]
        if changed:
            first = changed[0]
            flags.append(_flag("REGIME_MISMATCH", f"regime changed during trade: {entry_regime} -> "
                                                  f"{parse_details(first.get('details')).get('regime')} at "
                                                  f"{first['_t'].isoformat()} ({first.get('reason_code')}; "
                                                  f"{len(changed)} row(s))"))
    return flags, []


def classify_trade(trade: Mapping[str, Any], regime_rows: Sequence[Mapping[str, Any]],
                   events: Sequence[Mapping[str, Any]], open_issues: Mapping[str, set],
                   cfg: Mapping[str, Any]) -> dict:
    """All flags (with evidence), notes and the primary cause of one prepared trade."""
    r7 = trade.get("r7")
    comparable = bool(r7 and r7.get("comparable"))
    notes = list(trade.get("prep_notes") or [])
    if r7 is None:
        notes.append("INSUFFICIENT_DATA: no R7 row; fill-based rules not evaluated")
    elif not comparable:
        notes.append(f"INSUFFICIENT_DATA: R7 row not comparable ({r7.get('reason')}); fill-based rules not evaluated")
    if trade.get("net_r") is None:
        notes.append("INSUFFICIENT_DATA: r_multiple missing")
    flags = _execution_flags(trade, r7, comparable, open_issues, cfg)
    news_flags, news_notes = _news(trade, events, cfg)
    cost_flags, cost_notes, cost_r = _cost(trade, r7, comparable, cfg)
    regime_flags, regime_notes = _regime(trade, regime_rows, cfg)
    flags += news_flags + cost_flags + regime_flags
    notes += news_notes + cost_notes + regime_notes
    present = {f["cause"] for f in flags}
    primary = next((c for c in PRECEDENCE if c in present), DEFAULT_CAUSE)
    return {
        "trade_id": trade["trade_id"], "symbol": trade["symbol"], "direction": trade.get("direction"),
        "status": trade.get("status"), "source": trade.get("source"),
        "decision_time": _iso(trade.get("decision_time")), "entry_time": _iso(trade.get("entry_time")),
        "exit_time": _iso(trade.get("exit_time")), "iso_week": iso_week(trade.get("exit_time")),
        "net_r": trade.get("net_r"), "profit_loss": trade.get("profit_loss"),
        "stop_pips": trade.get("stop_pips"), "stop_source": trade.get("stop_source"), "cost_r": cost_r,
        "r7_reason": None if r7 is None else (r7.get("reason") if not comparable else "COMPARABLE"),
        "primary_cause": primary, "flags": flags, "notes": notes,
    }


# ----------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------
def funnel_rejections(funnel: Sequence[Mapping[str, Any]], start: pd.Timestamp | None = None,
                      end: pd.Timestamp | None = None) -> list[dict]:
    counts: dict[tuple[str, str], int] = {}
    for row in funnel:
        if row.get("outcome") != "REJECT" or row.get("_t") is None:
            continue
        if start is not None and not (start <= row["_t"] < end):
            continue
        key = (str(row.get("stage")), str(row.get("reason_code")))
        counts[key] = counts.get(key, 0) + 1
    return [{"stage": k[0], "reason_code": k[1], "count": v}
            for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def week_summary(week: str, trades: Sequence[Mapping[str, Any]], funnel: Sequence[Mapping[str, Any]],
                 cfg: Mapping[str, Any]) -> dict:
    rows = [t for t in trades if t["iso_week"] == week]
    n = len(rows)
    counts = {c: sum(1 for t in rows if t["primary_cause"] == c) for c in CAUSES}
    minimum = int(cfg["min_trades_for_pattern"])
    if n < minimum:
        sample = f"INCONCLUSIVE: {n} closed trades; no pattern claims"
        patterns = None
    else:
        sample = f"{n} closed trades (>= min_trades_for_pattern {minimum}): causes with >= {PATTERN_SHARE:.0%} share listed"
        patterns = [{"cause": c, "trades": k, "share": round(k / n, 4)} for c, k in counts.items()
                    if k / n >= PATTERN_SHARE]
    start, end = week_bounds(week) if WEEK_RE.match(week) else (None, None)
    return {"week": week, "start": _iso(start), "end": _iso(end), "n_trades": n,
            "inconclusive": n < minimum, "sample": sample, "cause_counts": counts, "patterns": patterns,
            "trade_ids": [t["trade_id"] for t in rows],
            "funnel_rejections": funnel_rejections(funnel, start, end) if start is not None else []}


def _cell(text: Any) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def _hhmm(iso: str | None) -> str:
    return "n/a" if not iso else pd.Timestamp(iso).strftime("%Y-%m-%d %H:%M:%S")


def _header(title: str, sources: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any], version: Any, sha: str) -> list[str]:
    lines = [title, "", REPORT_ONLY, "", NOTE, "", "## Data sources", "", "| source | path | sha256_16 |",
             "| --- | --- | --- |"]
    for s in sources:
        lines.append(f"| {s['name']} | `{s['path']}` | {s.get('sha256_16') or 'n/a'} |")
    lines += ["", f"Thresholds: version {version}, sha256_16 {sha}: "
                  + ", ".join(f"{k}={v}" for k, v in cfg.items()) + ".", "",
              "Precedence of the primary cause: " + " > ".join(PRECEDENCE) + f" > {DEFAULT_CAUSE} "
              f"({DEFAULT_CAUSE}: {NOISE_MEANING}). All flags are kept.", ""]
    return lines


def _funnel_table(rejections: Sequence[Mapping[str, Any]]) -> list[str]:
    if not rejections:
        return ["No REJECT rows in the decision funnel for this period."]
    lines = ["| stage | reason_code | count |", "| --- | --- | --- |"]
    lines += [f"| {r['stage']} | {r['reason_code']} | {r['count']} |" for r in rejections]
    return lines


def render_week(summary: Mapping[str, Any], trades: Sequence[Mapping[str, Any]], report: Mapping[str, Any]) -> str:
    lines = _header(f"# Trade postmortem {summary['week']} (R9)", report["sources"], report["cfg"],
                    report["thresholds_version"], report["thresholds_sha256_16"])
    lines += [f"Week {summary['week']}: exit times in [{summary['start']}, {summary['end']}).", "",
              f"**{summary['sample']}**", "", "## Primary cause counts", "", "| cause | trades |", "| --- | --- |"]
    lines += [f"| {c} | {k} |" for c, k in summary["cause_counts"].items()]
    if summary["patterns"] is not None:
        lines += ["", f"## Patterns (causes with >= {PATTERN_SHARE:.0%} of the week's trades)", ""]
        lines += [f"- {p['cause']}: {p['trades']}/{summary['n_trades']} ({p['share']:.0%})" for p in summary["patterns"]]
    lines += ["", "## Trades", "",
              "| trade_id | symbol | direction | entry UTC | exit UTC | net R | primary cause | flags (evidence) | notes |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for t in trades:
        if t["iso_week"] != summary["week"]:
            continue
        flags = "; ".join(f"{f['cause']}: {f['evidence']}" for f in t["flags"]) or "none"
        net = "n/a" if t["net_r"] is None else f"{t['net_r']:+.3f}"
        lines.append(f"| {t['trade_id']} | {t['symbol']} | {t['direction']} | {_hhmm(t['entry_time'])} | "
                     f"{_hhmm(t['exit_time'])} | {net} | {t['primary_cause']} | {_cell(flags)} | "
                     f"{_cell('; '.join(t['notes']) or '')} |")
    lines += ["", "## Decision funnel rejections (stage, reason_code)", ""]
    lines += _funnel_table(summary["funnel_rejections"])
    return "\n".join(lines) + "\n"


def render_none(report: Mapping[str, Any]) -> str:
    lines = _header("# Trade postmortem (R9): no closed trades", report["sources"], report["cfg"],
                    report["thresholds_version"], report["thresholds_sha256_16"])
    lines += ["**INCONCLUSIVE: 0 closed trades; no pattern claims**", "",
              "## Decision funnel rejections (stage, reason_code), all rows read", ""]
    lines += _funnel_table(report["funnel_rejections_all"])
    return "\n".join(lines) + "\n"


def build_report(trades: Sequence[Mapping[str, Any]], funnel: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any],
                 *, thresholds_version: Any, thresholds_sha: str, sources: Sequence[Mapping[str, Any]],
                 week: str | None = None) -> dict:
    weeks = sorted({t["iso_week"] for t in trades})
    selected = [week] if week else weeks
    return {
        "tool": "r9_trade_postmortem", "note": NOTE, "report_only": REPORT_ONLY,
        "precedence": list(PRECEDENCE) + [DEFAULT_CAUSE], "ordinary_noise_meaning": NOISE_MEANING,
        "pattern_share": PATTERN_SHARE, "cfg": dict(cfg), "thresholds_version": thresholds_version,
        "thresholds_sha256_16": thresholds_sha, "sources": list(sources), "selected_weeks": selected,
        "weeks": {w: week_summary(w, trades, funnel, cfg) for w in sorted(set(weeks) | set(selected))},
        "funnel_rejections_all": funnel_rejections(funnel),
        "trades": list(trades),
    }


def run(db: Path, deals_path: Path, bars_dir: Path, *, exits_dir: Path | None = None,
        feedback_dir: Path | None = None, calendar: Path = DEFAULT_CALENDAR, thresholds: Path | None = None,
        evidence_stream: str = DEFAULT_STREAM, week: str | None = None) -> dict:
    config, sha = load_thresholds(thresholds)
    cfg = config["r9_postmortem"]
    deals, _ = load_deals(deals_path)
    exits = load_exits(exits_dir) if exits_dir else {}
    feedback = load_feedback_files(feedback_dir) if feedback_dir else None
    events = load_calendar_events(calendar)
    conn = open_ro(db)
    try:
        closed = closed_trades(conn, evidence_stream, exits=exits, deals=deals)
        r7_rows = reconcile(conn, deals, load_m15(bars_dir), feedback_files=feedback,
                            evidence_stream=evidence_stream, cost=COST_SCENARIOS["base"])
        funnel = _with_time(load_funnel(conn, evidence_stream))
        issues = load_open_issues(conn)
        max_id = conn.execute("SELECT MAX(id) FROM trades").fetchone()[0]
    finally:
        conn.close()
    regime_rows = [r for r in funnel if r.get("stage") == "REGIME"]
    trades = [classify_trade(t, regime_rows, events, issues, cfg) for t in prepare_trades(closed, r7_rows, funnel)]
    trades.sort(key=lambda t: (t["exit_time"] or "", t["trade_id"]))
    bars_sha, n_bars = _sha16_dir(bars_dir, "bars_*_M15.json")
    exits_sha, n_exits = _sha16_dir(exits_dir, "exit_*.json") if exits_dir else (None, 0)
    fb_sha, n_fb = _sha16_dir(feedback_dir, "*.json") if feedback_dir else (None, 0)
    sources = [
        {"name": f"state DB (read-only, max trades.id {max_id}, stream {evidence_stream})", "path": str(db),
         "sha256_16": _sha16_file(db)},
        {"name": "deals.json copy", "path": str(deals_path), "sha256_16": _sha16_file(deals_path)},
        {"name": f"M15 bars ({n_bars} files)", "path": str(bars_dir), "sha256_16": bars_sha},
        {"name": f"exit files ({n_exits})", "path": str(exits_dir) if exits_dir else None, "sha256_16": exits_sha},
        {"name": f"feedback files ({n_fb})", "path": str(feedback_dir) if feedback_dir else None, "sha256_16": fb_sha},
        {"name": f"economic calendar ({len(events)} events)", "path": str(calendar), "sha256_16": _sha16_file(calendar)},
        {"name": "thresholds", "path": str(thresholds) if thresholds else "research/live_vs_replay_thresholds.json",
         "sha256_16": sha},
    ]
    return build_report(trades, funnel, cfg, thresholds_version=config["version"], thresholds_sha=sha,
                        sources=sources, week=week)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R9 signal postmortem of closed demo trades (offline, report only)")
    parser.add_argument("--db", type=Path, required=True, help="state DB (opened read-only, mode=ro)")
    parser.add_argument("--deals", type=Path, required=True, help="copy of the bridge market/deals.json")
    parser.add_argument("--bars-dir", type=Path, required=True, help="directory with bars_<SYM>_M15.json copies")
    parser.add_argument("--exits-dir", type=Path, default=None, help="copies of bridge exits/exit_*.json")
    parser.add_argument("--feedback-dir", type=Path, default=None, help="execution_*.json copies (recursive)")
    parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR)
    parser.add_argument("--week", default=None, help="ISO week YYYY-Www (default: every week present)")
    parser.add_argument("--evidence-stream", default=DEFAULT_STREAM)
    parser.add_argument("--thresholds", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.week is not None and not WEEK_RE.match(args.week):
        parser.error(f"--week must look like 2026-W45, got {args.week!r}")

    out = guard_output_dir(args.out or ARTIFACTS / "r9" / run_id())
    report = run(args.db, args.deals, args.bars_dir, exits_dir=args.exits_dir, feedback_dir=args.feedback_dir,
                 calendar=args.calendar, thresholds=args.thresholds, evidence_stream=args.evidence_stream,
                 week=args.week)
    report["metadata"] = {"run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    out.mkdir(parents=True, exist_ok=True)
    written = []
    if not report["trades"] and args.week is None:
        md = render_none(report)
        (out / "postmortem_none.md").write_text(md, encoding="utf-8")
        written.append(md)
    else:
        for week in report["selected_weeks"]:
            md = render_week(report["weeks"][week], report["trades"], report)
            (out / f"postmortem_{week}.md").write_text(md, encoding="utf-8")
            written.append(md)
    write_json(out / "postmortem.json", report)
    for md in written:
        print(md)
    print(f"Outputs: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
