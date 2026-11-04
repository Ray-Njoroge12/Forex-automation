"""R8: live-vs-replay drift monitor for the demo account.
Places the realised demo R sequence (closed strategy trades, in exit-time order)
inside bands simulated from the replay R distribution with wave 1's resampler
(research.monte_carlo.resample), and applies the predefined "investigate"
thresholds from research/live_vs_replay_thresholds.json (section r8_drift_monitor,
frozen before any real data was read; rationale in ADR 0004).
Report only: no halt or veto authority, and nothing in the engine reads this
output. INVESTIGATE asks a human to look; it is not a trading signal. Fewer live
trades than ``min_live_trades`` give INCONCLUSIVE, with any flags shown as
informational only. A replay distribution that has not passed the R2 gate is
labelled NOT EVIDENCE.
Live R is net R computed from the position's deals (research.demo_records.R_DEFINITION),
the same definition as the replay's r_multiple (net_usd / risk_usd). The EA's own
r_multiple is price-based and leaves out commission and swap, so it is not used.
Trades without a closing deal or without the data for risk in USD are left out and
counted in the report.
Usage (from fx_ai_engine/):
    USE_MT5_MOCK=1 ../.venv/bin/python -m research.drift_monitor --db <db> --replay-trades-csv <csv> --deals <deals.json> [--exits-dir <dir>] [--gate-verdict <verdict.json>] --out <dir>
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
import numpy as np
import pandas as pd
from research import demo_records as dr
from research.common import ARTIFACTS, run_id, write_json
from research.monte_carlo import PERCENTILES, resample
REPORT_ONLY = "Report only: no halt or veto authority; INVESTIGATE asks a human to look."
HEADER_NOTE = ("Live demo R sequence placed inside bands simulated from the replay R distribution "
               "(research.monte_carlo.resample); thresholds from research/live_vs_replay_thresholds.json.")
UNGATED_NOTE = "replay distribution not gated by R2 (NOT EVIDENCE)"
VERDICTS = ("INCONCLUSIVE", "INVESTIGATE", "WITHIN_BANDS")
FLAG_RULES = (
    # (metric, threshold key in cfg, direction, flag)
    ("cum_r", "cum_r_low_percentile", "below", "CUM_R_BELOW_BAND"),
    ("cum_r", "cum_r_high_percentile", "above", "CUM_R_ABOVE_BAND"),
    ("max_dd_r", "max_dd_r_high_percentile", "above", "DRAWDOWN_ABOVE_BAND"),
    ("loss_streak", "loss_streak_high_percentile", "above", "LOSS_STREAK_ABOVE_BAND"),
)
FLAG_NOTES = {"CUM_R_ABOVE_BAND": "check recording (live results better than almost every simulated path)"}
MD_EDGE_ROWS = 10  # markdown band table shows the first and last MD_EDGE_ROWS steps when longer than twice that
ROUND = 4


def _r(value: float) -> float:
    return round(float(value), ROUND)


def _sha16(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _pct(values: np.ndarray) -> dict[str, float]:
    return {f"p{p}": _r(np.percentile(values, p)) for p in PERCENTILES}


def _rank(simulated: np.ndarray, live: float) -> float:
    return _r(100.0 * np.mean(simulated <= live))


def max_drawdown_r(r: Sequence[float]) -> float:
    """Largest fall in R of the cumulative curve that starts at 0 (the 0 start counts as a peak)."""
    return float(_max_dd_paths(np.asarray([list(r)], dtype=float))[0])


def longest_loss_streak(r: Sequence[float]) -> int:
    """Longest run of consecutive trades with r < 0."""
    return int(_loss_streak_paths(np.asarray([list(r)], dtype=float))[0])


def _max_dd_paths(draws: np.ndarray) -> np.ndarray:
    cum = np.concatenate([np.zeros((draws.shape[0], 1)), draws.cumsum(axis=1)], axis=1)
    return (np.maximum.accumulate(cum, axis=1) - cum).max(axis=1)


def _loss_streak_paths(draws: np.ndarray) -> np.ndarray:
    current = np.zeros(draws.shape[0], dtype=int)
    best = np.zeros(draws.shape[0], dtype=int)
    for j in range(draws.shape[1]):
        current = np.where(draws[:, j] < 0, current + 1, 0)
        best = np.maximum(best, current)
    return best


def load_config(path: str | Path | None = None) -> tuple[dict, int, str]:
    """(r8_drift_monitor section, thresholds version, sha256_16) from the versioned thresholds file."""
    config, sha = dr.load_thresholds(path)
    return dict(config["r8_drift_monitor"]), int(config["version"]), sha


def drift_report(replay_r: Sequence[float], live_r: Sequence[float], cfg: Mapping[str, Any], *,
                 live_ids: Sequence[str] | None = None) -> dict[str, Any]:
    replay = np.asarray([float(x) for x in replay_r], dtype=float)
    if replay.size == 0:
        raise ValueError("no replay R values to resample")
    live = np.asarray([float(x) for x in live_r], dtype=float)
    n = int(live.size)
    ids = [str(x) for x in live_ids] if live_ids is not None else [None] * n
    if len(ids) != n:
        raise ValueError("live_ids and live_r differ in length")
    min_n = int(cfg["min_live_trades"])
    report: dict[str, Any] = {
        "tool": "r8_drift_monitor", "note": HEADER_NOTE, "report_only": REPORT_ONLY, "cfg": dict(cfg),
        "n_live": n, "min_live_trades": min_n, "replay_trades": int(replay.size),
        "replay_mean_r": _r(replay.mean()), "live_mean_r": _r(live.mean()) if n else None,
    }
    if n == 0:
        return {**report, "verdict": "INCONCLUSIVE", "reason": "NO_CLOSED_LIVE_TRADES",
                "flags": [], "flags_informational": False, "metrics": {}, "bands": []}

    draws = resample(replay, n, int(cfg["runs"]), str(cfg["method"]), int(cfg["block_length"]),
                     np.random.default_rng(cfg["seed"]))
    cum = draws.cumsum(axis=1)
    live_cum = live.cumsum()
    bands = []
    for k in range(1, n + 1):
        sim_k = cum[:, k - 1]
        bands.append({"k": k, "trade_id": ids[k - 1], "live_r": _r(live[k - 1]), **_pct(sim_k),
                      "live_cum_r": _r(live_cum[k - 1]), "live_rank": _rank(sim_k, live_cum[k - 1])})

    simulated = {"cum_r": cum[:, -1], "max_dd_r": _max_dd_paths(draws),
                 "loss_streak": _loss_streak_paths(draws).astype(float)}
    live_values = {"cum_r": float(live_cum[-1]), "max_dd_r": max_drawdown_r(live),
                   "loss_streak": float(longest_loss_streak(live))}
    metrics = {m: {"live": _r(live_values[m]), "simulated": _pct(simulated[m]),
                   "live_rank": _rank(simulated[m], live_values[m]),
                   "live_rank_strict": _r(100.0 * np.mean(simulated[m] < live_values[m]))} for m in simulated}

    # Ties go against firing: a "below" flag uses P(sim <= live), an "above" flag P(sim < live),
    # so R values on a lattice (e.g. only +2.2 / -1.0) cannot fire a flag by tying the simulated mode.
    flag_rows, flags = [], []
    for metric, key, direction, flag in FLAG_RULES:
        threshold = float(cfg[key])
        rank = metrics[metric]["live_rank" if direction == "below" else "live_rank_strict"]
        fired = rank < threshold if direction == "below" else rank > threshold
        flag_rows.append({"flag": flag, "metric": metric,
                          "rule": f"100*P(sim <= live) < {key}" if direction == "below"
                          else f"100*P(sim < live) > {key}",
                          "threshold_percentile": threshold, "live_rank": rank, "fired": bool(fired),
                          "note": FLAG_NOTES.get(flag)})
        if fired:
            flags.append(flag)

    if n < min_n:
        verdict, reason, informational = "INCONCLUSIVE", f"N_LIVE_BELOW_MIN ({n} < {min_n})", True
    elif flags:
        verdict, reason, informational = "INVESTIGATE", ", ".join(flags), False
    else:
        verdict, reason, informational = "WITHIN_BANDS", "no flag fired", False
    return {**report, "verdict": verdict, "reason": reason, "flags": flags, "flags_informational": informational,
            "flag_table": flag_rows, "metrics": metrics, "bands": bands}


BAND_COLUMNS = ("k", "trade_id", "live_r", *[f"p{p}" for p in PERCENTILES], "live_cum_r", "live_rank")


def _fmt(value: Any, fmt: str = "+.3f") -> str:
    return "n/a" if value is None else format(value, fmt)


def render_markdown(report: Mapping[str, Any]) -> str:
    cfg = report["cfg"]
    gate = report.get("replay_gate") or {}
    lines = ["# Live vs replay drift (R8)", "", report["report_only"], "", report["note"], "",
             f"**Verdict: {report['verdict']}** ({report['reason']})", "",
             f"Live closed trades: {report['n_live']} (minimum for a verdict: {report['min_live_trades']}). "
             f"Replay trades: {report['replay_trades']}. Mean R live {_fmt(report['live_mean_r'], '+.4f')} vs "
             f"replay {_fmt(report['replay_mean_r'], '+.4f')}.",
             f"Simulation: {cfg['runs']} paths, method {cfg['method']}, block length {cfg['block_length']}, "
             f"seed {cfg['seed']}. Thresholds version {report.get('thresholds_version')} "
             f"(sha256_16 {report.get('thresholds_sha256_16')}).", ""]
    if report.get("r_definition"):
        lines += [f"R definition (live and replay): {report['r_definition']}", ""]
    if gate:
        lines += [f"Replay gate: {gate.get('note') or 'R2 verdict ' + str(gate.get('verdict'))} "
                  f"(evidence status {gate.get('evidence_status')}).", ""]
        if gate.get("binding"):
            lines += [f"Gate binding: {gate['binding']}: {gate.get('binding_note')}", ""]
    inputs = report.get("inputs")
    if inputs:
        excluded = inputs.get("without_net_r") or {}
        lines += [f"Inputs: DB `{inputs['db']}` (stream {inputs['evidence_stream']}, read-only); replay "
                  f"`{inputs['replay_trades_csv']}` (sha256_16 {inputs['replay_trades_sha256_16']}, "
                  f"{inputs['replay_trade_count']} trades). Closed live trades read {inputs['closed_trades']}, "
                  f"left out for lack of net R {sum(excluded.values())}"
                  + (f" ({', '.join(f'{k}: {v}' for k, v in sorted(excluded.items()))})" if excluded else "") + ".",
                  ""]
    if not report["bands"]:
        return "\n".join(lines) + "\n"
    if report["flags_informational"]:
        lines += ["Flags below are informational only: the live sample is under the minimum.", ""]
    lines += ["| flag | rule | threshold | live rank | fired | note |", "| --- | --- | --- | --- | --- | --- |"]
    for row in report["flag_table"]:
        lines.append(f"| {row['flag']} | {row['rule']} | {row['threshold_percentile']:g} | {row['live_rank']:.2f} | "
                     f"{'yes' if row['fired'] else 'no'} | {row['note'] or ''} |")
    lines += ["", "| metric | live | " + " | ".join(f"p{p}" for p in PERCENTILES)
              + " | 100*P(sim <= live) | 100*P(sim < live) |",
              "| --- | --- | " + " | ".join("---" for _ in PERCENTILES) + " | --- | --- |"]
    for name, m in report["metrics"].items():
        sim = m["simulated"]
        lines.append(f"| {name} | {m['live']:+.3f} | " + " | ".join(f"{sim[f'p{p}']:+.3f}" for p in PERCENTILES)
                     + f" | {m['live_rank']:.2f} | {m['live_rank_strict']:.2f} |")
    bands = report["bands"]
    shown = bands if len(bands) <= 2 * MD_EDGE_ROWS else [*bands[:MD_EDGE_ROWS], None, *bands[-MD_EDGE_ROWS:]]
    lines += ["", "Cumulative R bands per live trade (full table in bands.csv):", "",
              "| k | " + " | ".join(f"p{p}" for p in PERCENTILES) + " | live cum R | live rank |",
              "| --- | " + " | ".join("---" for _ in PERCENTILES) + " | --- | --- |"]
    for row in shown:
        if row is None:
            lines.append("| ... | " + " | ".join("..." for _ in PERCENTILES) + " | ... | ... |")
            continue
        lines.append(f"| {row['k']} | " + " | ".join(f"{row[f'p{p}']:+.2f}" for p in PERCENTILES)
                     + f" | {row['live_cum_r']:+.2f} | {row['live_rank']:.2f} |")
    return "\n".join(lines) + "\n"


def load_live(db: Path, evidence_stream: str, exits_dir: Path | None, deals_path: Path,
              units: dr.AccountUnits | None = None) -> tuple[list[str], list[float], dict[str, Any]]:
    """Live net R (demo_records.R_DEFINITION) of closed strategy trades in final-exit-time order."""
    exits = dr.load_exits(exits_dir, units) if exits_dir else None
    deals = dr.load_deals(deals_path, units)[0]
    conn = dr.open_ro(db)
    try:
        closed = dr.closed_trades(conn, evidence_stream, exits=exits, deals=deals, units=units)
        max_id = conn.execute("SELECT MAX(id) FROM trades").fetchone()[0]
    finally:
        conn.close()
    usable = [t for t in closed if t.get("net_r") is not None]
    usable.sort(key=lambda t: (t["exit_time"] is None, t["exit_time"] or pd.Timestamp(0, tz="UTC"), t["id"]))
    excluded: dict[str, int] = {}
    for t in closed:
        if t.get("net_r") is None:
            excluded[t.get("net_r_missing") or "unknown"] = excluded.get(t.get("net_r_missing") or "unknown", 0) + 1
    ea_gap = [float(t["r_multiple"]) - float(t["net_r"]) for t in usable if t.get("r_multiple") is not None]
    meta = {"closed_trades": len(closed), "without_net_r": excluded,
            "mean_ea_r_minus_net_r": _r(np.mean(ea_gap)) if ea_gap else None,
            "exit_time_unverified": sum(1 for t in usable if t.get("exit_time_unverified")),
            "without_exit_time": sum(1 for t in usable if t.get("exit_time") is None),
            "sources": {s: sum(1 for t in usable if t["source"] == s) for s in sorted({t["source"] for t in usable})},
            "max_trades_id_read": max_id}
    return [str(t["trade_id"]) for t in usable], [float(t["net_r"]) for t in usable], meta


def replay_row_count(path: Path) -> int:
    return int(len(pd.read_csv(path)))


def load_replay(path: Path) -> list[float]:
    trades = pd.read_csv(path)
    trades = trades.dropna(subset=["r_multiple"])
    trades = trades.assign(_t=pd.to_datetime(trades["exit_time"], utc=True)).sort_values("_t", kind="mergesort")
    return trades["r_multiple"].astype(float).tolist()


def load_gate(path: Path | None, replay_csv: Path | None = None) -> dict[str, Any]:
    """R2 verdict for the replay, checked against the replay CSV being resampled.

    R2's verdict.json binds to the policy hash and the bar datasets, not to a trades CSV. The check
    uses what it records: a replay-trades sha256 prefix when present (``replay_trades_sha256_16``),
    else its trade count against the CSV's row count. A mismatch makes the bands NOT_EVIDENCE; a
    matching count without a hash is reported as UNVERIFIED for a human to confirm.
    """
    if path is None:
        return {"path": None, "verdict": None, "evidence_status": "NOT_EVIDENCE", "note": UNGATED_NOTE,
                "binding": None, "binding_note": None}
    verdict = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {"path": str(path), "verdict": verdict.get("verdict"), "evidence_status": verdict.get("evidence_status"),
           "note": None, "binding": None, "binding_note": None}
    if replay_csv is None:
        return out
    csv_sha, csv_rows = _sha16(replay_csv), replay_row_count(replay_csv)
    gate_sha, gate_trades = verdict.get("replay_trades_sha256_16"), verdict.get("trades")
    if gate_sha is not None:
        bound = gate_sha == csv_sha
        out["binding"] = "BOUND" if bound else "MISMATCH"
        out["binding_note"] = f"R2 replay_trades_sha256_16 {gate_sha} vs CSV {csv_sha}"
    elif gate_trades is not None and int(gate_trades) != csv_rows:
        out["binding"] = "MISMATCH"
        out["binding_note"] = f"R2 verdict covers {gate_trades} trades, the replay CSV has {csv_rows} rows"
    else:
        out["binding"] = "UNVERIFIED"
        out["binding_note"] = ("R2 verdict.json records no replay-trades hash"
                               + (f"; its trade count matches the CSV ({csv_rows})" if gate_trades is not None else
                                  " and no trade count")
                               + "; confirm by hand that it was produced for this replay")
    if out["binding"] == "MISMATCH":
        out["evidence_status"] = "NOT_EVIDENCE"
        out["note"] = "R2 verdict does not belong to this replay CSV (NOT EVIDENCE)"
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R8 live-vs-replay drift monitor (offline, report only)")
    parser.add_argument("--db", type=Path, required=True, help="state DB (opened read-only, mode=ro)")
    parser.add_argument("--replay-trades-csv", type=Path, required=True, help="replay trades CSV with r_multiple, exit_time")
    parser.add_argument("--exits-dir", type=Path, default=None, help="copies of bridge exits/exit_*.json")
    parser.add_argument("--deals", type=Path, required=True,
                        help="copy of the bridge market/deals.json (live net R is computed from the deals)")
    parser.add_argument("--gate-verdict", type=Path, default=None, help="R2 verdict.json for the replay")
    parser.add_argument("--evidence-stream", default=dr.DEFAULT_STREAM)
    parser.add_argument("--thresholds", type=Path, default=None)
    parser.add_argument("--account-currency", default=None,
                        help="account currency of the bridge copies (default: status.json beside deals, else USD)")
    parser.add_argument("--symbols", type=Path, default=None,
                        help="symbols.json copy for contract sizes (default: beside the deals file)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    out = dr.guard_output_dir(args.out or ARTIFACTS / "r8" / run_id())
    cfg, version, sha = load_config(args.thresholds)
    units = dr.resolve_units(args.deals, args.account_currency, args.symbols)
    ids, live_r, live_meta = load_live(args.db, args.evidence_stream, args.exits_dir, args.deals, units)
    replay_r = load_replay(args.replay_trades_csv)
    report = drift_report(replay_r, live_r, cfg, live_ids=ids)
    report["thresholds_version"] = version
    report["thresholds_sha256_16"] = sha
    report["r_definition"] = dr.R_DEFINITION
    report["replay_gate"] = load_gate(args.gate_verdict, args.replay_trades_csv)
    report["inputs"] = {
        "db": str(args.db), "evidence_stream": args.evidence_stream, **live_meta,
        "exits_dir": str(args.exits_dir) if args.exits_dir else None,
        "deals": str(args.deals), "deals_sha256_16": _sha16(args.deals),
        "replay_trades_csv": str(args.replay_trades_csv), "replay_trades_sha256_16": _sha16(args.replay_trades_csv),
        "replay_trade_count": len(replay_r),
        "thresholds": str(args.thresholds) if args.thresholds else "research/live_vs_replay_thresholds.json",
    }
    if not units.is_default:
        report["inputs"]["units"] = units.describe()
    write_json(out / "drift_monitor.json", report)
    md = render_markdown(report)
    (out / "drift_monitor.md").write_text(md, encoding="utf-8")
    pd.DataFrame(report["bands"], columns=list(BAND_COLUMNS)).to_csv(out / "bands.csv", index=False)
    print(md)
    print(f"Replay gate: {report['replay_gate']['verdict']} ({report['replay_gate']['evidence_status']})")
    print(f"Outputs: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
