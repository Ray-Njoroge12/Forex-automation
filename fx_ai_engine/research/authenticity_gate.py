"""R2: replay authenticity gate. One ordered pass/fail audit over a replay result.

Checks, in order (``blocking`` checks decide the verdict):
  1. R1A_TRUNCATION                     R1 (a)
  2. R1B_FUTURE_CORRUPTION              R1 (b), random and nan modes
  3. R1C_WARMUP_PARITY                  R1 (c1)
  4. R1C_WARMUP_DEVIATION (non-blocking) R1 (c2); shared by live and replay
  5. R2_NO_SAME_BAR_FILL                entry fills at the open of the first bar after the decision
  6. R2_FILLS_WITHIN_BID_ASK            entry, partial and exit prices inside the modelled quote range
  7. R2_NO_FILL_IN_GAP_OR_CLOSED_MARKET fill bars exist and are outside the FX weekly close
  8. R4_COST_STRESS                     fixed-trade-set expectancy still > 0 at the stress multiple

A replay report is evidence only with a PASS verdict from this gate whose
policy_hash and dataset hashes match the report. The verdict file says so
in ``evidence_status`` and the markdown's first line.

Usage (from fx_ai_engine/):
    USE_MT5_MOCK=1 ../.venv/bin/python -m research.authenticity_gate \
        --bridge-dir tests/fixtures/live_bars_2026-11-03 --max-decisions 60
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backtesting.unified_replay import (
    COST_SCENARIOS, EXIT_MODES, SYMBOLS, Candidate, CostModel, Frames, SimulationResult, pip_size,
)
from core.policy.envelope import canonical_policy_hash
from core.strategy import DEFAULT_STRATEGY_ID
from research import bias_audit, cost_stress
from research.common import (
    ARTIFACTS, add_source_args, add_strategy_args, dataset_hashes, load_frames, parse_ts, policy_for_variant,
    resolve_strategy_args, run_id, run_replay_in_process, source_dir, write_json,
)

CHECK_ORDER = (
    "R1A_TRUNCATION", "R1B_FUTURE_CORRUPTION", "R1C_WARMUP_PARITY", "R1C_WARMUP_DEVIATION",
    "R2_NO_SAME_BAR_FILL", "R2_FILLS_WITHIN_BID_ASK", "R2_NO_FILL_IN_GAP_OR_CLOSED_MARKET", "R4_COST_STRESS",
)
NON_BLOCKING = {"R1C_WARMUP_DEVIATION"}
MAX_LISTED = 20
PRICE_EPS = 1e-9
PRINT_TOL = 5e-6  # event prices are printed with 5 decimals
GAP_MINUTES = 60
BAR = pd.Timedelta(minutes=15)
EVENT_RE = re.compile(r"^([A-Z_]+)@([0-9.]+)x([0-9.]+)$")
NEW_YORK = ZoneInfo("America/New_York")
NOT_EVIDENCE_LINE = "# NOT EVIDENCE: replay failed the authenticity gate"
PASS_LINE = "# Replay authenticity gate: PASS"


def _check(check_id: str, ok: bool, detail: Sequence[Any], **extra) -> dict[str, Any]:
    return {"id": check_id, "status": "PASS" if ok else "FAIL", "blocking": check_id not in NON_BLOCKING,
            "detail": list(detail)[:MAX_LISTED], **extra}


def _bar_index(frames: Frames, symbol: str) -> pd.DataFrame:
    return frames[symbol]["M15"]


def _quote(df: pd.DataFrame, start: pd.Timestamp, symbol: str, cost: CostModel) -> dict[str, float] | None:
    if start not in df.index:
        return None
    row = df.loc[start]
    spread = cost.spread_pips(symbol, float(row["spread_points"])) * pip_size(symbol)
    return {"bid_open": float(row["open"]), "bid_high": float(row["high"]), "bid_low": float(row["low"]),
            "ask_high": float(row["high"]) + spread, "ask_low": float(row["low"]) + spread,
            "spread": spread, "spread_points": float(row["spread_points"])}


# ----------------------------------------------------------------------
# Fill checks
# ----------------------------------------------------------------------
def check_same_bar_fill(trades: Sequence[Mapping[str, Any]], candidates: Sequence[Candidate], frames: Frames,
                        cost: CostModel) -> dict[str, Any]:
    by_symbol: dict[str, list[pd.Timestamp]] = {}
    for c in candidates:
        by_symbol.setdefault(c.symbol, []).append(pd.Timestamp(c.decision_time))
    for times in by_symbol.values():
        times.sort()
    bad = []
    for t in trades:
        symbol, entry = t["symbol"], pd.Timestamp(t["entry_time"])
        times = by_symbol.get(symbol, [])
        k = int(np.searchsorted(np.array([x.value for x in times], dtype="int64"), entry.value, side="right")) - 1
        if k < 0:
            bad.append({"trade_id": t["trade_id"], "reason": "no decision at or before entry"})
            continue
        decision = times[k]
        df = _bar_index(frames, symbol)
        j = int(df.index.searchsorted(decision, side="left"))
        if j >= len(df) or df.index[j] != entry:
            bad.append({"trade_id": t["trade_id"], "reason": "entry bar is not the first bar after the decision",
                        "decision_time": decision.isoformat(), "entry_time": entry.isoformat()})
            continue
        pip = pip_size(symbol)
        bid_open = float(df["open"].iat[j])
        spread = cost.spread_pips(symbol, float(df["spread_points"].iat[j])) * pip
        expected = bid_open + spread + cost.slippage * pip if t["direction"] == "BUY" else bid_open - cost.slippage * pip
        if abs(float(t["entry_price"]) - expected) > PRICE_EPS:
            bad.append({"trade_id": t["trade_id"], "reason": "entry price is not the next-bar open quote",
                        "entry_price": float(t["entry_price"]), "expected": expected})
    return _check("R2_NO_SAME_BAR_FILL", not bad, bad, checked=len(trades), offenders=len(bad))


def _in(price: float, low: float, high: float) -> bool:
    return low <= price <= high


def _side_range(q: Mapping[str, float], side: str, slip: float, tol: float) -> tuple[float, float]:
    """Buying fills on the ask (slippage above), selling on the bid (slippage below)."""
    if side == "buy":
        return q["ask_low"] - tol, q["ask_high"] + slip + tol
    return q["bid_low"] - slip - tol, q["bid_high"] + tol


def check_fills_within_bid_ask(trades: Sequence[Mapping[str, Any]], frames: Frames, cost: CostModel) -> dict[str, Any]:
    tol = PRICE_EPS + PRINT_TOL
    bad = []
    for t in trades:
        symbol = t["symbol"]
        df = _bar_index(frames, symbol)
        slip = cost.slippage * pip_size(symbol)
        entry_side, exit_side = ("buy", "sell") if t["direction"] == "BUY" else ("sell", "buy")
        entry_t = pd.Timestamp(t["entry_time"])
        exit_start = pd.Timestamp(t["exit_time"]) - BAR
        q_entry, q_exit = _quote(df, entry_t, symbol, cost), _quote(df, exit_start, symbol, cost)
        if q_entry is None or q_exit is None:
            bad.append({"trade_id": t["trade_id"], "reason": "fill bar missing (see gap check)"})
            continue
        lo, hi = _side_range(q_entry, entry_side, slip, tol)
        if not _in(float(t["entry_price"]), lo, hi):
            bad.append({"trade_id": t["trade_id"], "fill": "ENTRY", "price": float(t["entry_price"]), "range": [lo, hi]})
        lo, hi = _side_range(q_exit, exit_side, slip, tol)
        if not _in(float(t["exit_price"]), lo, hi):
            bad.append({"trade_id": t["trade_id"], "fill": t["exit_reason"], "price": float(t["exit_price"]),
                        "range": [lo, hi]})
        partials = [m for m in (EVENT_RE.match(e) for e in str(t.get("events", "")).split(";") if e) if m
                    and m.group(1) == "PARTIAL"]
        if partials:
            life = [q for q in (_quote(df, s, symbol, cost) for s in df.index[(df.index >= entry_t) & (df.index <= exit_start)]) if q]
            env = {"bid_low": min(q["bid_low"] for q in life), "bid_high": max(q["bid_high"] for q in life),
                   "ask_low": min(q["ask_low"] for q in life), "ask_high": max(q["ask_high"] for q in life)}
            lo, hi = _side_range(env, exit_side, slip, tol)
            for m in partials:
                if not _in(float(m.group(2)), lo, hi):
                    bad.append({"trade_id": t["trade_id"], "fill": "PARTIAL", "price": float(m.group(2)),
                                "range": [lo, hi], "note": "checked against the trade's lifetime envelope"})
    return _check("R2_FILLS_WITHIN_BID_ASK", not bad, bad, checked=len(trades), offenders=len(bad))


def is_fx_market_closed(ts: pd.Timestamp) -> bool:
    """FX weekly close: Friday 17:00 to Sunday 17:00 America/New_York."""
    local = pd.Timestamp(ts).tz_convert(NEW_YORK)
    wd, hour = local.weekday(), local.hour
    return wd == 5 or (wd == 4 and hour >= 17) or (wd == 6 and hour < 17)


def check_no_fill_in_gap_or_closed_market(trades: Sequence[Mapping[str, Any]], frames: Frames) -> dict[str, Any]:
    bad = []
    after_gap = 0
    for t in trades:
        symbol = t["symbol"]
        idx = _bar_index(frames, symbol).index
        entry_t = pd.Timestamp(t["entry_time"])
        exit_start = pd.Timestamp(t["exit_time"]) - BAR
        for label, start in (("entry", entry_t), ("exit", exit_start)):
            if start not in idx:
                bad.append({"trade_id": t["trade_id"], "fill": label, "reason": "no M15 bar for the fill",
                            "bar_start": start.isoformat()})
            elif is_fx_market_closed(start):
                bad.append({"trade_id": t["trade_id"], "fill": label, "reason": "bar inside the FX weekly close",
                            "bar_start": start.isoformat()})
        j = idx.searchsorted(entry_t)
        if 0 < j < len(idx) and idx[j] == entry_t and idx[j] - idx[j - 1] > pd.Timedelta(minutes=GAP_MINUTES):
            after_gap += 1
    return _check("R2_NO_FILL_IN_GAP_OR_CLOSED_MARKET", not bad, bad, checked=len(trades), offenders=len(bad),
                  entries_after_data_gap=after_gap, gap_minutes=GAP_MINUTES)


# ----------------------------------------------------------------------
# R1 and R4 mapping
# ----------------------------------------------------------------------
def _r1_checks(r1: Mapping[str, Any]) -> list[dict[str, Any]]:
    tr, fc, wp, wd = (r1[k] for k in ("truncation", "future_corruption", "warmup_parity", "warmup_deviation"))
    dev = {"id": "R1C_WARMUP_DEVIATION", "status": wd["status"], "blocking": False,
           "detail": [{"symbol": e["symbol"], "tf": e["tf"], "status": e["status"]} for e in wd["frames"]][:MAX_LISTED],
           "note": "indicator warm-up is shared by live and replay; reported, not blocking"}
    return [
        _check("R1A_TRUNCATION", tr["status"] == "PASS",
               [{"symbol": s.get("symbol"), "checked": s["checked"], "mismatches": s["mismatches"]}
                for s in tr.get("by_symbol", [])] or [{"reason": tr.get("reason")}]),
        _check("R1B_FUTURE_CORRUPTION", fc["status"] == "PASS",
               [{"mode": m, "checked": fc[m].get("checked"), "mismatches": fc[m].get("mismatches")} for m in ("random", "nan")]),
        _check("R1C_WARMUP_PARITY", wp["status"] == "PASS",
               [{"symbol": p["symbol"], "status": p["status"], "reasons": p["reasons"]} for p in wp["by_symbol"]],
               live_fetch_bars=wp["live_fetch_bars"]),
        dev,
    ]


def _r4_check(trades: Sequence[Mapping[str, Any]], frames: Frames, cost: CostModel, min_multiple: float) -> dict[str, Any]:
    """Multiples are absolute: the trades' realised R is taken at cost.multiplier and re-costed from there."""
    m0 = float(cost.multiplier)
    rc = cost_stress.recost_trades(trades, frames, cost, sorted({1.0, m0, float(min_multiple)}))
    if rc.get("no_trades"):
        return _check("R4_COST_STRESS", False, [{"reason": "R4_NO_TRADES"}], recost=rc, min_breakeven_multiple=min_multiple)
    by_m = {row["multiple"]: row["expectancy_r"] for row in rc["rows"]}
    at_stress = by_m[float(min_multiple)]
    ok = at_stress is not None and at_stress > 0
    detail = [{"replay_cost_multiplier": m0, "expectancy_r_at_1x": by_m[1.0],
               f"expectancy_r_at_{min_multiple:g}x": at_stress, "breakeven_multiple": rc["breakeven_label"]}]
    return _check("R4_COST_STRESS", ok, detail, recost=rc, min_breakeven_multiple=min_multiple)


# ----------------------------------------------------------------------
# Verdict
# ----------------------------------------------------------------------
def build_verdict(checks: Sequence[Mapping[str, Any]], *, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    by_id = {c["id"]: dict(c) for c in checks}
    missing = [i for i in CHECK_ORDER if i not in by_id]
    ordered = [by_id[i] for i in CHECK_ORDER if i in by_id]
    reasons = [f"{c['id']}: {c['status']}" for c in ordered if c["blocking"] and c["status"] != "PASS"]
    reasons += [f"{i}: MISSING" for i in missing]
    warnings = [f"{c['id']}: {c['status']}" for c in ordered if not c["blocking"] and c["status"] != "PASS"]
    passed = not reasons
    return {"tool": "r2_authenticity_gate", "verdict": "PASS" if passed else "FAIL",
            "evidence_status": "EVIDENCE_ELIGIBLE" if passed else "NOT_EVIDENCE",
            "reasons": reasons, "warnings": warnings, "checks": ordered, **dict(context or {})}


def render_gate_markdown(verdict: Mapping[str, Any]) -> str:
    passed = verdict["verdict"] == "PASS"
    lines = [PASS_LINE if passed else NOT_EVIDENCE_LINE, ""]
    ctx = {k: verdict.get(k) for k in ("variant", "exit_mode", "cost", "policy_hash") if verdict.get(k) is not None}
    if ctx:
        lines.append("Bound to: " + ", ".join(f"{k} `{v}`" for k, v in ctx.items()) + ".")
    if verdict.get("dataset_sha256_16"):
        lines.append("Datasets: " + ", ".join(f"{k}={v}" for k, v in verdict["dataset_sha256_16"].items()) + ".")
    lines += ["", f"Evidence status: **{verdict['evidence_status']}**.",
              "A replay report counts as evidence only with a PASS verdict whose policy_hash and dataset hashes match it.",
              "", "| # | check | status | blocking |", "| --- | --- | --- | --- |"]
    for n, c in enumerate(verdict["checks"], 1):
        lines.append(f"| {n} | {c['id']} | {c['status']} | {'yes' if c['blocking'] else 'no'} |")
    if verdict["reasons"]:
        lines += ["", "Blocking failures:", ""] + [f"- {r}" for r in verdict["reasons"]]
    if verdict["warnings"]:
        lines += ["", "Non-blocking:", ""] + [f"- {w}" for w in verdict["warnings"]]
    return "\n".join(lines) + "\n"


def audit_replay(*, frames: Frames, policy: Mapping[str, object], cost: CostModel, candidates: Sequence[Candidate],
                 result: SimulationResult, exit_mode: str = "policy", r1_report: Mapping[str, Any] | None = None,
                 max_decisions: int = 60, seed: int = 7, min_breakeven_multiple: float = 2.0,
                 context: Mapping[str, Any] | None = None, strategy_id: str = DEFAULT_STRATEGY_ID,
                 strategy_params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Gate one replay result. Integration hook for run_replay (see docs/research/validation-wave1-integration-notes.md).

    ``strategy_id``/``strategy_params`` are used only when ``r1_report`` is None (R1 is then run on that strategy).
    """
    if r1_report is None:
        r1_report = bias_audit.run_bias_audit(frames, policy, COST_SCENARIOS["base"], max_decisions=max_decisions,
                                              seed=seed, strategy_id=strategy_id, strategy_params=strategy_params)
    trades = result.trades
    checks = _r1_checks(r1_report) + [
        check_same_bar_fill(trades, candidates, frames, cost),
        check_fills_within_bid_ask(trades, frames, cost),
        check_no_fill_in_gap_or_closed_market(trades, frames),
        _r4_check(trades, frames, cost, min_breakeven_multiple),
    ]
    ctx = {"policy_hash": canonical_policy_hash(policy), "exit_mode": exit_mode, "cost": cost.name,
           "trades": len(trades), "r1_verdict": r1_report.get("verdict"), **dict(context or {})}
    return build_verdict(checks, context=ctx)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R2 replay authenticity gate (offline, read-only)")
    add_source_args(parser)
    parser.add_argument("--exit-mode", default="policy", choices=EXIT_MODES)
    parser.add_argument("--cost", default="base", choices=sorted(COST_SCENARIOS))
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-decisions", type=int, default=60)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--min-breakeven-multiple", type=float, default=2.0)
    parser.add_argument("--out", type=Path, default=None)
    add_strategy_args(parser)
    args = parser.parse_args(argv)
    strategy_params = resolve_strategy_args(parser, args)

    frames = load_frames(bridge_dir=args.bridge_dir, data_dir=args.data_dir, symbols=args.symbols)
    policy, _ = policy_for_variant(args.variant)
    cost = COST_SCENARIOS[args.cost]
    start, end = parse_ts(args.start), parse_ts(args.end)
    strategy_kw = {"strategy_id": args.strategy, "strategy_params": strategy_params}
    candidates, _, result = run_replay_in_process(frames, policy, cost, exit_mode=args.exit_mode, start=start, end=end,
                                                  **strategy_kw)
    r1 = bias_audit.run_bias_audit(frames, policy, COST_SCENARIOS["base"], start=start, end=end,
                                   max_decisions=args.max_decisions, seed=args.seed, **strategy_kw)
    verdict = audit_replay(frames=frames, policy=policy, cost=cost, candidates=candidates, result=result,
                           exit_mode=args.exit_mode, r1_report=r1, min_breakeven_multiple=args.min_breakeven_multiple,
                           context={"variant": args.variant, "source": str(source_dir(args)), **strategy_kw,
                                    "dataset_sha256_16": dataset_hashes(source_dir(args)),
                                    "symbols": [s for s in SYMBOLS if s in frames]})
    out = Path(args.out or ARTIFACTS / "r2" / run_id())
    write_json(out / "verdict.json", verdict)
    (out / "gate_report.md").write_text(render_gate_markdown(verdict), encoding="utf-8")
    print(f"R2 authenticity gate verdict: {verdict['verdict']} ({verdict['evidence_status']})")
    for c in verdict["checks"]:
        print(f"  {c['id']:36} {c['status']:12} {'blocking' if c['blocking'] else 'non-blocking'}")
    for r in verdict["reasons"]:
        print(f"  reason: {r}")
    print(f"Outputs: {out}")
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
