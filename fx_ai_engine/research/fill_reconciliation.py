"""R7: fill reconciliation of demo deals against the replay cost and fill model.

Offline, read-only, report-only. For each broker entry deal of the six majors
the tool compares the actual fill (price, spread at fill, slippage, timing,
commission, exit slippage) with what backtesting.unified_replay would have
assumed for the same signal, reports the bias (mean, distribution, seeded
bootstrap CI) and emits a suggested cost-model adjustment as data only.
unified_replay.COST_SCENARIOS is never changed. No halt or veto authority.

Conventions: all differences are in pips (unified_replay.pip_size); sign s = +1
for BUY and -1 for SELL; a positive difference is worse for the trader than
the model. The replay fills a candidate decided at M15 bar close T at the open
of the bar whose open == T: BUY = bid_open + modelled spread + slippage, SELL =
bid_open - slippage. The strict reference (replay_execution.market_entry) uses
the first bar that opens strictly after the decision time; it is reported per
deal only and does not enter the verdict.

Usage (from fx_ai_engine/):
    USE_MT5_MOCK=1 ../.venv/bin/python -m research.fill_reconciliation --db <db> --deals <deals.json> --bars-dir <dir> [--feedback-dir <dir>] --out <dir>
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from backtesting.replay_execution import QuoteBar, market_entry
from backtesting.unified_replay import COST_SCENARIOS, SYMBOLS, CostModel, pip_size, pip_value_usd_per_lot
from research.common import ARTIFACTS, parse_ts, run_id, write_json
from research.demo_records import (
    DEFAULT_STREAM, EA_MAGIC, Deal, guard_output_dir, resolve_units, load_deals, load_execution_feedback, load_feedback_files,
    load_funnel, load_m15, load_thresholds, load_trades, open_ro,
)

REPORT_ONLY = "Report only: no halt or veto authority; the cost model is unchanged."
HEADER_NOTE = ("Offline comparison of demo broker fills with the replay cost and fill model "
               "(backtesting.unified_replay). Positive differences are worse for the trader than the model.")
ADJUSTMENT_NOTE = "data only; unified_replay.COST_SCENARIOS is not changed by this tool"
DEMO_CAVEAT = ("MetaQuotes-Demo spreads and fills differ from a funded broker's (plan Phase 6); "
               "re-measure on the funded account before using any suggestion.")
METRICS = ("fill_diff_pips", "spread_diff_pips", "slippage_diff_pips", "exit_slippage_diff_pips",
           "fill_latency_s", "commission_diff_usd_per_lot")
# fill_latency_s is measured against a model latency of 0 s, so it is positive by construction and would
# fix the verdict at SYSTEMATIC_BIAS; it is reported with its own status but kept out of the verdict.
INFORMATIONAL_METRICS = ("fill_latency_s",)
VERDICT_METRICS = tuple(m for m in METRICS if m not in INFORMATIONAL_METRICS)
VERDICTS = ("INCONCLUSIVE", "SYSTEMATIC_BIAS", "NO_SYSTEMATIC_BIAS")
EXIT_TAG = re.compile(r"\[(sl|tp)\s+([0-9]*\.?[0-9]+)\]", re.IGNORECASE)
PRICE_MISMATCH_PIPS = 0.05
BAR = pd.Timedelta(minutes=15)
OPEN_STATUSES = ("EXECUTED_OPEN",)
CSV_COLUMNS = (
    "trade_id", "deal_ticket", "position_id", "symbol", "direction", "volume", "comparable", "reason",
    "decision_time", "decision_time_source", "bar_close", "fill_time", "signal_latency_s", "fill_latency_s",
    "route_to_fill_s", "deal_price", "bid_open", "bar_spread_points", "modelled_spread_pips", "replay_fill",
    "actual_spread_pips", "actual_spread_source", "ea_slippage_pips", "fill_diff_pips", "spread_diff_pips",
    "implied_slippage_pips", "slippage_diff_pips", "strict_fill", "strict_diff_pips", "exit_deal_ticket",
    "exit_time", "exit_price", "exit_magic", "exit_kind", "exit_level", "exit_slippage_pips", "model_exit_slippage_pips",
    "exit_slippage_diff_pips", "commission_per_lot_usd", "commission_diff_usd_per_lot", "live_cost_pips",
    "model_cost_pips", "cost_ratio", "price_mismatch_pips",
)


def _r(value: float | None, nd: int = 6) -> float | None:
    return None if value is None else round(float(value), nd) + 0.0  # + 0.0 turns -0.0 into 0.0


def _sha16(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def parse_exit_comment(comment: str | None) -> tuple[str | None, float | None]:
    """'[sl 1.11936]' -> ('STOP', 1.11936); '[tp X]' -> ('TARGET', X); otherwise (None, None)."""
    match = EXIT_TAG.search(comment or "")
    if not match:
        return None, None
    return ("STOP" if match.group(1).lower() == "sl" else "TARGET"), float(match.group(2))


def _router_times(conn: sqlite3.Connection, evidence_stream: str) -> dict[str, pd.Timestamp]:
    out: dict[str, pd.Timestamp] = {}
    for row in load_funnel(conn, evidence_stream, stage="ROUTER"):
        if row.get("outcome") != "ROUTED" or not row.get("trade_id"):
            continue
        ts = parse_ts(str(row["decision_time"]))
        if ts is not None:
            out.setdefault(str(row["trade_id"]), ts)
    return out


def _base_row(deal: Deal, trade_id: str | None, direction: str) -> dict[str, Any]:
    row: dict[str, Any] = {c: None for c in CSV_COLUMNS}
    row.update({"trade_id": trade_id, "deal_ticket": deal.ticket, "position_id": deal.position_id,
                "symbol": deal.symbol, "direction": direction, "volume": deal.volume, "comparable": False,
                "fill_time": deal.time.isoformat(), "deal_price": deal.price})
    return row


def _exit_fields(row: dict, deal: Deal, exits: Sequence[Deal], s: int, pip: float, cost: CostModel) -> None:
    if not exits:
        return
    last = exits[-1]
    kind, level = parse_exit_comment(last.comment)
    if kind is None:
        kind = "NOT_BY_EA" if last.magic != EA_MAGIC else "UNTAGGED"
    row.update({"exit_deal_ticket": last.ticket, "exit_time": last.time.isoformat(), "exit_price": last.price,
                "exit_magic": last.magic, "exit_kind": kind, "exit_level": level})
    if level is not None:
        slip = s * (level - last.price) / pip
        model = cost.slippage if kind == "STOP" else 0.0
        row.update({"exit_slippage_pips": _r(slip), "model_exit_slippage_pips": _r(model),
                    "exit_slippage_diff_pips": _r(slip - model)})
    commission = deal.commission + sum(x.commission for x in exits)
    if deal.volume > 0:
        per_lot = -commission / deal.volume
        row.update({"commission_per_lot_usd": _r(per_lot), "commission_diff_usd_per_lot": _r(per_lot - cost.commission)})


def _strict_reference(row: dict, frame: pd.DataFrame, decision_time: pd.Timestamp, direction: str, s: int,
                      deal: Deal, pip: float, cost: CostModel) -> None:
    later = frame[frame.index > decision_time]
    if later.empty:
        return
    t, bar = later.index[0], later.iloc[0]
    spread = cost.spread_pips(deal.symbol, float(bar["spread_points"])) * pip
    try:
        quote = QuoteBar(start=t, end=t + BAR, bid_open=float(bar["open"]), bid_high=float(bar["high"]),
                         bid_low=float(bar["low"]), bid_close=float(bar["close"]), ask_open=float(bar["open"]) + spread,
                         ask_high=float(bar["high"]) + spread, ask_low=float(bar["low"]) + spread,
                         ask_close=float(bar["close"]) + spread)
        fill = market_entry(direction=direction, submitted_at=decision_time, quote=quote, pip_size=pip,
                            slippage_pips=cost.slippage)
    except ValueError:
        return
    if fill is not None:
        row.update({"strict_fill": _r(fill.price, 6), "strict_diff_pips": _r(s * (deal.price - fill.price) / pip)})


def reconcile(conn: sqlite3.Connection, deals: Sequence[Deal], m15: Mapping[str, pd.DataFrame], *,
              feedback_files: Mapping[str, Mapping[str, Any]] | None = None,
              evidence_stream: str = DEFAULT_STREAM, cost: CostModel = COST_SCENARIOS["base"]) -> list[dict]:
    """One row per entry deal of the six majors, plus one 'NO_DEAL' row per executed trade without a deal."""
    trades = load_trades(conn, evidence_stream)
    by_id = {str(t["trade_id"]): t for t in trades if t.get("trade_id")}
    by_ticket = {int(t["trade_ticket"]): t for t in trades if t.get("trade_ticket")}
    feedback_db = load_execution_feedback(conn)
    files = dict(feedback_files or {})
    files_by_ticket = {int(v.get("ticket") or 0): v for v in files.values() if int(v.get("ticket") or 0)}
    routed = _router_times(conn, evidence_stream)
    exits_by_position: dict[int, list[Deal]] = {}
    for d in deals:
        if d.entry == 1 and d.position_id:
            exits_by_position.setdefault(d.position_id, []).append(d)

    rows: list[dict] = []
    matched: set[str] = set()
    for deal in deals:
        if deal.entry != 0 or deal.symbol not in SYMBOLS:
            continue
        direction = "BUY" if deal.type == 0 else "SELL"
        s = 1 if direction == "BUY" else -1
        pip = pip_size(deal.symbol)
        trade = by_id.get(deal.comment) or by_ticket.get(deal.ticket)
        trade_id = str(trade["trade_id"]) if trade else (deal.comment or None)
        row = _base_row(deal, trade_id, direction)
        exits = sorted(exits_by_position.get(deal.position_id, []), key=lambda x: (x.time, x.ticket))
        _exit_fields(row, deal, exits, s, pip, cost)
        rows.append(row)
        if trade is None:
            row["reason"] = "NO_STRATEGY_SIGNAL"
            continue
        matched.add(trade_id)

        feedback: Mapping[str, Any] | None = None
        if trade_id in feedback_db:
            feedback = feedback_db[trade_id].get("payload") or {}
        elif trade_id in files:
            feedback = files[trade_id]
        elif deal.ticket in files_by_ticket:
            feedback = files_by_ticket[deal.ticket]

        decision_time, source = routed.get(trade_id), "ROUTER"
        if decision_time is None and trade.get("open_time"):
            decision_time, source = parse_ts(str(trade["open_time"])), "trades.open_time"
        if decision_time is None:
            row["reason"] = "NO_DECISION_RECORD"
            continue
        bar_close = decision_time.floor("15min")
        row.update({"decision_time": decision_time.isoformat(), "decision_time_source": source,
                    "bar_close": bar_close.isoformat(),
                    "signal_latency_s": _r((decision_time - bar_close).total_seconds(), 3),
                    "fill_latency_s": _r((deal.time - bar_close).total_seconds(), 3),
                    "route_to_fill_s": _r((deal.time - decision_time).total_seconds(), 3)})

        refs = [x for x in (_num(trade.get("entry_price")), _num((feedback or {}).get("entry_price"))) if x]
        worst = max((abs(deal.price - x) / pip for x in refs), default=0.0)
        if worst > PRICE_MISMATCH_PIPS:
            row["price_mismatch_pips"] = _r(worst)

        frame = m15.get(deal.symbol)
        if frame is None or bar_close not in frame.index:
            row["reason"] = "NO_BAR_DATA"
            continue
        bar = frame.loc[bar_close]
        bid_open, spread_points = float(bar["open"]), float(bar["spread_points"])
        modelled = cost.spread_pips(deal.symbol, spread_points)
        replay_fill = bid_open + (modelled + cost.slippage) * pip if s == 1 else bid_open - cost.slippage * pip
        row.update({"comparable": True, "bid_open": bid_open, "bar_spread_points": spread_points,
                    "modelled_spread_pips": _r(modelled), "replay_fill": _r(replay_fill, 8),
                    "fill_diff_pips": _r(s * (deal.price - replay_fill) / pip)})

        spread_price = _num((feedback or {}).get("spread_at_entry"))
        spread_source = "feedback" if spread_price is not None else None
        if spread_price is None and _num(trade.get("spread_exec_price")) is not None:
            spread_price, spread_source = _num(trade.get("spread_exec_price")), "trades.spread_exec_price"
        ea_slip = _num((feedback or {}).get("slippage"))
        row["ea_slippage_pips"] = _r(ea_slip / pip) if ea_slip is not None else None
        actual = spread_price / pip if spread_price is not None else None
        row.update({"actual_spread_pips": _r(actual), "actual_spread_source": spread_source})
        implied = None
        if actual is not None:
            row["spread_diff_pips"] = _r(actual - modelled)
        if s == -1 or actual is not None:
            ask_or_bid = bid_open + (actual * pip if s == 1 else 0.0)
            implied = s * (deal.price - ask_or_bid) / pip
            row.update({"implied_slippage_pips": _r(implied), "slippage_diff_pips": _r(implied - cost.slippage)})

        _strict_reference(row, frame, decision_time, direction, s, deal, pip, cost)

        per_lot = row["commission_per_lot_usd"]
        if actual is not None and implied is not None and per_lot is not None:
            pv = pip_value_usd_per_lot(deal.symbol, deal.price)
            exit_slip = row["exit_slippage_pips"] or 0.0
            live = actual + max(0.0, implied) + max(0.0, exit_slip) + per_lot / pv
            model = modelled + cost.slippage + (cost.slippage if row["exit_kind"] == "STOP" else 0.0) + cost.commission / pv
            row.update({"live_cost_pips": _r(live), "model_cost_pips": _r(model),
                        "cost_ratio": _r(live / model) if model > 0 else None})

    for trade in trades:
        status = str(trade.get("status") or "")
        if str(trade.get("trade_id")) in matched or not (status.startswith("CLOSED_") or status in OPEN_STATUSES):
            continue
        row = {c: None for c in CSV_COLUMNS}
        row.update({"trade_id": trade.get("trade_id"), "symbol": trade.get("symbol"),
                    "direction": trade.get("direction"), "volume": trade.get("lot_size"), "comparable": False,
                    "reason": "NO_DEAL", "deal_price": _num(trade.get("entry_price"))})
        rows.append(row)
    return rows


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------
def bootstrap_mean_ci(values: Sequence[float], runs: int, seed: int, ci_level: float) -> tuple[float, float]:
    x = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, x.size, size=(int(runs), x.size))].mean(axis=1)
    tail = (1.0 - float(ci_level)) / 2.0 * 100.0
    return float(np.percentile(means, tail)), float(np.percentile(means, 100.0 - tail))


def describe(values: Sequence[float], th: Mapping[str, Any]) -> dict[str, Any]:
    x = np.asarray([v for v in values if v is not None], dtype=float)
    n = int(x.size)
    if n == 0:
        return {"n": 0, "status": "INCONCLUSIVE", "reason": "no comparable values"}
    lo, hi = bootstrap_mean_ci(x, th["bootstrap_runs"], th["bootstrap_seed"], th["ci_level"])
    out = {"n": n, "mean": float(x.mean()), "median": float(np.median(x)),
           "std": float(x.std(ddof=1)) if n > 1 else 0.0,
           **{f"p{p}": float(np.percentile(x, p)) for p in (5, 25, 75, 95)},
           "min": float(x.min()), "max": float(x.max()), "ci_low": lo, "ci_high": hi}
    out = {k: (round(v, 6) + 0.0 if isinstance(v, float) else v) for k, v in out.items()}
    if n < int(th["min_deals_for_bias"]):
        out.update(status="INCONCLUSIVE", reason=f"n={n} < min_deals_for_bias={th['min_deals_for_bias']}")
    elif out["ci_low"] > 0 or out["ci_high"] < 0:
        out["status"] = "SYSTEMATIC_BIAS"
    else:
        out["status"] = "NO_SYSTEMATIC_BIAS"
    return out


def summarise(rows: Sequence[Mapping[str, Any]], thresholds_r7: Mapping[str, Any],
              cost: CostModel = COST_SCENARIOS["base"]) -> dict[str, Any]:
    th = thresholds_r7
    comparable = [r for r in rows if r.get("comparable")]
    symbols = [s for s in SYMBOLS if any(r["symbol"] == s for r in comparable)]
    metrics = {}
    for name in METRICS:
        metrics[name] = {"overall": describe([r.get(name) for r in comparable], th),
                         "by_symbol": {s: describe([r.get(name) for r in comparable if r["symbol"] == s], th)
                                       for s in symbols}}
    statuses = [metrics[name]["overall"]["status"] for name in VERDICT_METRICS]
    if all(st == "INCONCLUSIVE" for st in statuses):
        verdict = "INCONCLUSIVE"
    elif "SYSTEMATIC_BIAS" in statuses:
        verdict = "SYSTEMATIC_BIAS"
    else:
        verdict = "NO_SYSTEMATIC_BIAS"

    ratios = [r["cost_ratio"] for r in comparable if r.get("cost_ratio") is not None]
    limit = float(th["cost_ratio_limit"])
    cost_ratio: dict[str, Any] = {"n": len(ratios), "limit": limit,
                                  "mean": round(float(np.mean(ratios)), 6) if ratios else None}
    if len(ratios) < int(th["min_deals_for_bias"]):
        cost_ratio["status"] = "INCONCLUSIVE"
    else:
        cost_ratio["status"] = "WITHIN_LIMIT" if cost_ratio["mean"] <= limit else "ABOVE_LIMIT"

    implied = [r["implied_slippage_pips"] for r in comparable if r.get("implied_slippage_pips") is not None]
    commissions = [r["commission_per_lot_usd"] for r in comparable if r.get("commission_per_lot_usd") is not None]
    pct = float(th["suggested_spread_percentile"])
    floors = {}
    for s in symbols:
        spreads = [r["actual_spread_pips"] for r in comparable if r["symbol"] == s and r.get("actual_spread_pips") is not None]
        if spreads:
            floors[s] = round(float(np.percentile(spreads, pct)), 4)
    suggestion = {
        "actionable": verdict != "INCONCLUSIVE",
        "note": ADJUSTMENT_NOTE,
        "caveat": DEMO_CAVEAT,
        "slippage_pips": round(max(0.0, float(np.mean(implied))), 4) if implied else None,
        "commission_per_lot_usd": round(float(np.mean(commissions)), 4) if commissions else None,
        "spread_floor_pips": floors,
        "spread_floor_percentile": pct,
        "current": {"cost_scenario": cost.name, "multiplier": cost.multiplier, "slippage_pips": cost.slippage_pips,
                    "commission_per_lot_usd": cost.commission_per_lot_usd,
                    "spread_floor_pips": {s: float(cost.spread_floor_pips.get(s, 1.0)) for s in (symbols or SYMBOLS)}},
    }
    reasons: dict[str, int] = {}
    for r in rows:
        if not r.get("comparable"):
            reasons[r.get("reason") or "UNKNOWN"] = reasons.get(r.get("reason") or "UNKNOWN", 0) + 1
    strict = [r["strict_diff_pips"] for r in comparable if r.get("strict_diff_pips") is not None]
    return {
        "tool": "r7_fill_reconciliation", "note": HEADER_NOTE, "report_only": REPORT_ONLY,
        "cost_scenario": cost.name,
        "cost_model": {"multiplier": cost.multiplier, "slippage_pips": cost.slippage,
                       "commission_per_lot_usd": cost.commission},
        "thresholds_r7": dict(th),
        "counts": {"rows": len(rows), "entry_deals": sum(1 for r in rows if r.get("deal_ticket") is not None),
                   "comparable": len(comparable), "not_comparable": reasons},
        "verdict": verdict,
        "verdict_metrics": list(VERDICT_METRICS),
        "informational_metrics": list(INFORMATIONAL_METRICS),
        "metrics": metrics,
        "cost_ratio": cost_ratio,
        "strict_reference": {"n": len(strict), "mean_strict_diff_pips": round(float(np.mean(strict)), 6) if strict else None,
                             "note": "replay_execution.market_entry on the first bar opening after the decision; "
                                     "informational, not in the verdict"},
        "suggested_adjustment": suggestion,
        "not_comparable": [{"trade_id": r.get("trade_id"), "deal_ticket": r.get("deal_ticket"),
                            "symbol": r.get("symbol"), "reason": r.get("reason")}
                           for r in rows if not r.get("comparable")],
    }


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------
def _f(value: Any, fmt: str = "+.3f") -> str:
    return "n/a" if value is None else format(value, fmt)


def render_markdown(report: Mapping[str, Any], rows: Sequence[Mapping[str, Any]] | None = None) -> str:
    c = report["counts"]
    lines = [
        "# Fill reconciliation (R7)", "", report["report_only"], "", report["note"], "",
        f"Cost scenario `{report['cost_scenario']}` (slippage {report['cost_model']['slippage_pips']:.2f} pip, "
        f"commission {report['cost_model']['commission_per_lot_usd']:.2f} USD/lot round trip). "
        f"Entry deals {c['entry_deals']}, comparable {c['comparable']}, not comparable "
        f"{', '.join(f'{k} {v}' for k, v in sorted(c['not_comparable'].items())) or 'none'}.",
        "",
        f"**Verdict: {report['verdict']}** (per metric: INCONCLUSIVE below "
        f"{report['thresholds_r7']['min_deals_for_bias']} comparable deals; SYSTEMATIC_BIAS when the "
        f"{report['thresholds_r7']['ci_level']:.0%} bootstrap CI of the mean excludes 0; the verdict is "
        f"SYSTEMATIC_BIAS if any of {', '.join(report['verdict_metrics'])} has it). "
        f"Informational, not in the verdict: {', '.join(report['informational_metrics'])} (the replay assumes "
        f"a fill at the bar close, 0 s, so fill latency is positive by construction; read its size).",
        "",
        "| metric | n | mean | median | std | p5 | p95 | CI of mean | status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, m in report["metrics"].items():
        o = m["overall"]
        ci = f"[{_f(o.get('ci_low'))}, {_f(o.get('ci_high'))}]" if o["n"] else "n/a"
        lines.append(f"| {name} | {o['n']} | {_f(o.get('mean'))} | {_f(o.get('median'))} | {_f(o.get('std'), '.3f')} | "
                     f"{_f(o.get('p5'))} | {_f(o.get('p95'))} | {ci} | {o['status']}"
                     f"{' (informational)' if name in report['informational_metrics'] else ''} |")
    by_sym = {s for m in report["metrics"].values() for s in m["by_symbol"]}
    if by_sym:
        lines += ["", "Per symbol (mean, n, status):", "",
                  "| symbol | " + " | ".join(report["metrics"]) + " |",
                  "| --- |" + " --- |" * len(report["metrics"])]
        for s in [x for x in SYMBOLS if x in by_sym]:
            cells = []
            for m in report["metrics"].values():
                o = m["by_symbol"].get(s, {"n": 0, "status": "INCONCLUSIVE"})
                cells.append(f"{_f(o.get('mean'))} (n={o['n']}, {o['status']})")
            lines.append(f"| {s} | " + " | ".join(cells) + " |")
    cr = report["cost_ratio"]
    lines += ["", f"Cost ratio live/model (Phase 6 criterion: within {cr['limit']}x): mean {_f(cr['mean'], '.3f')} "
                  f"over {cr['n']} deals, {cr['status']}."]
    st = report["strict_reference"]
    lines += [f"Strict reference (market_entry, next bar after the decision): mean diff {_f(st['mean_strict_diff_pips'])} "
              f"pip over {st['n']} deals; informational only."]
    adj = report["suggested_adjustment"]
    cur = adj["current"]
    lines += ["", f"## Suggested cost-model adjustment ({adj['note']})", "",
              f"Actionable: {adj['actionable']}. {adj['caveat']}", "",
              "| parameter | current | suggested |", "| --- | --- | --- |",
              f"| slippage_pips | {cur['slippage_pips']} | {_f(adj['slippage_pips'], '.4f')} |",
              f"| commission_per_lot_usd | {cur['commission_per_lot_usd']} | {_f(adj['commission_per_lot_usd'], '.4f')} |"]
    for s, v in adj["spread_floor_pips"].items():
        lines.append(f"| spread_floor_pips {s} (p{adj['spread_floor_percentile']:g} of actual) | "
                     f"{cur['spread_floor_pips'].get(s)} | {v:.4f} |")
    if report["not_comparable"]:
        lines += ["", "## Not comparable", "", "| trade_id | deal | symbol | reason |", "| --- | --- | --- | --- |"]
        for r in report["not_comparable"]:
            lines.append(f"| {r['trade_id']} | {r['deal_ticket'] or ''} | {r['symbol']} | {r['reason']} |")
    if rows:
        lines += ["", "## Deals", "",
                  "| trade_id | symbol | dir | fill latency s | spread diff | slippage diff | fill diff | exit | exit slip diff | cost ratio |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            if r.get("deal_ticket") is None:
                continue
            lines.append(f"| {r['trade_id']} | {r['symbol']} | {r['direction']} | {_f(r.get('fill_latency_s'), '.1f')} | "
                         f"{_f(r.get('spread_diff_pips'))} | {_f(r.get('slippage_diff_pips'))} | {_f(r.get('fill_diff_pips'))} | "
                         f"{r.get('exit_kind') or 'open'} | {_f(r.get('exit_slippage_diff_pips'))} | {_f(r.get('cost_ratio'), '.3f')} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R7 fill reconciliation of demo deals vs the replay cost model (offline)")
    parser.add_argument("--db", type=Path, required=True, help="state DB (opened read-only, mode=ro)")
    parser.add_argument("--deals", type=Path, required=True, help="copy of the bridge market/deals.json")
    parser.add_argument("--bars-dir", type=Path, required=True, help="directory with bars_<SYM>_M15.json copies")
    parser.add_argument("--feedback-dir", type=Path, default=None, help="execution_*.json copies (recursive)")
    parser.add_argument("--evidence-stream", default=DEFAULT_STREAM)
    parser.add_argument("--cost-scenario", default="base", choices=sorted(COST_SCENARIOS))
    parser.add_argument("--thresholds", type=Path, default=None)
    parser.add_argument("--account-currency", default=None,
                        help="account currency of the bridge copies (default: status.json beside deals, else USD)")
    parser.add_argument("--symbols", type=Path, default=None,
                        help="symbols.json copy for contract sizes (default: beside the deals file)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    out = guard_output_dir(args.out or ARTIFACTS / "r7" / run_id())
    thresholds, thresholds_sha = load_thresholds(args.thresholds)
    cost = COST_SCENARIOS[args.cost_scenario]
    units = resolve_units(args.deals, args.account_currency, args.symbols)
    deals, deals_meta = load_deals(args.deals, units)
    m15 = load_m15(args.bars_dir)
    feedback = load_feedback_files(args.feedback_dir, units) if args.feedback_dir else None
    conn = open_ro(args.db)
    try:
        rows = reconcile(conn, deals, m15, feedback_files=feedback, evidence_stream=args.evidence_stream, cost=cost)
        max_id = conn.execute("SELECT MAX(id) FROM trades").fetchone()[0]
    finally:
        conn.close()
    report = summarise(rows, thresholds["r7_fill_reconciliation"], cost)
    report["thresholds_version"] = thresholds["version"]
    inputs = {"db": str(args.db), "max_trades_id_read": max_id, "evidence_stream": args.evidence_stream,
              "deals": str(args.deals), "deals_sha256_16": _sha16(args.deals),
              "deals_generated_at": deals_meta.get("generated_at"), "bars_dir": str(args.bars_dir),
              "bar_symbols": sorted(m15), "feedback_dir": str(args.feedback_dir) if args.feedback_dir else None,
              "thresholds": str(args.thresholds) if args.thresholds else "research/live_vs_replay_thresholds.json",
              "thresholds_sha256_16": thresholds_sha}
    if not units.is_default:
        inputs["units"] = units.describe()
    write_json(out / "fill_reconciliation.json", {**report, "inputs": inputs, "rows": rows})
    md = render_markdown(report, rows)
    (out / "fill_reconciliation.md").write_text(md, encoding="utf-8")
    pd.DataFrame(rows, columns=list(CSV_COLUMNS)).to_csv(out / "deals.csv", index=False)
    print(md)
    print(f"Outputs: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
