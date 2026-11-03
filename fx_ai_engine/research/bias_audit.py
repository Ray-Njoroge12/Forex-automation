"""R1: lookahead and recursive-bias audit of the replay decision path.

Checks, all on the same functions the live engine and the unified replay use
(``generate_candidates`` -> RegimeAgent/TechnicalAgent, then AdversarialAgent and
PortfolioManager):

  (a) truncation: the decision at bar close t is identical when every bar that
      has not closed by t is removed;
  (b) future corruption: randomising (``random``) or blanking (``nan``) those
      bars leaves the decision at t unchanged;
  (c1) warm-up parity: the replay feed serves min(requested, closed bars) for
      every request, so live and replay compute recursive indicators on the
      same windows;
  (c2) warm-up deviation: how far EMA/ADX/RSI computed on the live fetch
      length are from the same indicator computed on all history, and the
      lookback needed to stay under a threshold.

Usage (from fx_ai_engine/):
    USE_MT5_MOCK=1 ../.venv/bin/python -m research.bias_audit \
        --bridge-dir tests/fixtures/live_bars_2026-11-03 --max-decisions 60 --synthetic-long-bars 2400
"""
from __future__ import annotations

import argparse
import random
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from backtesting.unified_replay import (
    COST_SCENARIOS, SYMBOLS, TIMEFRAME_CODES, TIMEFRAME_NAMES, TIMEFRAME_SECONDS, CostModel, Frames,
    PointInTimeFeed, _bar_spread_price, generate_candidates, pip_size,
)
from core.account_status import AccountStatus
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.filters.session_filter import is_tradeable_session
from core.indicators.calculators import calculate_adx, calculate_ema, calculate_rsi
from core.policy.envelope import canonical_policy_hash
from core.types import RegimeOutput, TechnicalSignal
from research.common import (
    ARTIFACTS, add_source_args, decision_fingerprint, load_frames, parse_ts, policy_for_variant, run_id,
    source_dir, write_json,
)

OHLC = ["open", "high", "low", "close"]
MAX_LISTED = 20
DEFAULT_GRID = (200, 250, 300, 350, 500, 750, 1000, 1500, 2000)
# EMA in pips: 10% of the active 2-pip trend_distance_threshold on non-JPY pairs. ADX/RSI in points.
DEFAULT_THRESHOLDS = {"ema": 0.2, "adx": 0.5, "rsi": 0.5}
Decide = Callable[..., dict]


def _account() -> AccountStatus:
    return AccountStatus(balance=100_000.0, equity=100_000.0, peak_equity=100_000.0,
                         daily_anchor_equity=100_000.0, weekly_anchor_equity=100_000.0)


# ----------------------------------------------------------------------
# Decision path
# ----------------------------------------------------------------------
def replay_decision(frames: Frames, policy: Mapping[str, object], cost: CostModel, symbol: str,
                    t: pd.Timestamp, *, lookback_override: int | None = None) -> dict:
    """Fingerprint of the full decision at t: stage 1 as generate_candidates, stage 2 as simulate."""
    t = pd.Timestamp(t)
    cands, funnel = generate_candidates(symbol, frames, policy, cost, decision_times=[t],
                                        lookback_override=lookback_override)
    fp = decision_fingerprint(cands[0] if cands else None, funnel)
    if cands:
        cand = cands[0]
        feed = PointInTimeFeed(frames)
        feed.lookback_override = lookback_override
        feed.set_time(t)
        spread_fn = lambda s: _bar_spread_price(frames, feed, s, cost)
        signal = TechnicalSignal(**cand.signal)
        regime = RegimeOutput(**cand.regime)
        account = _account()
        adv = AdversarialAgent(symbol, feed.fetch_ohlc, spread_fn, policy=dict(policy)).evaluate(
            signal, account, TIMEFRAME_CODES["M15"], now_utc=t.to_pydatetime())
        pm = PortfolioManager(fetch_ohlc=feed.fetch_ohlc, policy=dict(policy)).evaluate(
            signal, adv, account, open_symbols=[], regime=regime)
        fp["stage2"] = [adv.approved, adv.reason_code, pm.approved, pm.reason_code, pm.final_risk_percent]
    return fp


def _safe(decide: Decide, *args, **kwargs) -> dict:
    try:
        return decide(*args, **kwargs)
    except Exception as exc:  # a crash on altered data counts as a mismatch
        return {"error": f"{type(exc).__name__}: {exc}"}


def _unclosed(df: pd.DataFrame, tf: str, t: pd.Timestamp) -> np.ndarray:
    return (df.index + pd.Timedelta(seconds=TIMEFRAME_SECONDS[tf])) > t


def truncate_frames(frames: Frames, t: pd.Timestamp) -> Frames:
    """Keep only bars with open + timeframe <= t (closed by t) in every timeframe."""
    return {s: {tf: df[~_unclosed(df, tf, t)].copy() for tf, df in tfs.items()} for s, tfs in frames.items()}


def corrupt_frames(frames: Frames, t: pd.Timestamp, mode: str, seed: int) -> Frames:
    """Alter every bar not closed by t (future and partially formed bars)."""
    if mode not in {"random", "nan"}:
        raise ValueError("mode must be 'random' or 'nan'")
    rng = np.random.default_rng([seed, int(pd.Timestamp(t).value // 10**9)])
    out: Frames = {}
    for s, tfs in frames.items():
        out[s] = {}
        for tf, df in tfs.items():
            df = df.copy()
            mask = _unclosed(df, tf, t)
            n = int(mask.sum())
            if n:
                if mode == "random":
                    factor = rng.uniform(0.9, 1.1, size=n)
                    df.loc[mask, OHLC] = df.loc[mask, OHLC].to_numpy() * factor[:, None]
                    df.loc[mask, "spread_points"] = rng.integers(0, 60, size=n).astype(float)
                else:
                    df.loc[mask, OHLC] = np.nan
            out[s][tf] = df
    return out


# ----------------------------------------------------------------------
# Sampling
# ----------------------------------------------------------------------
def _session_times(frames: Frames, symbol: str, start, end) -> list[pd.Timestamp]:
    times = frames[symbol]["M15"].index + pd.Timedelta(seconds=TIMEFRAME_SECONDS["M15"])
    out = []
    for t in times:
        if (start is not None and t < start) or (end is not None and t > end):
            continue
        if is_tradeable_session(t.to_pydatetime()):
            out.append(t)
    return out


def sample_decision_times(frames: Frames, policy: Mapping[str, object], cost: CostModel, symbol: str, *,
                          start=None, end=None, max_decisions: int = 60, seed: int = 7) -> dict[str, Any]:
    """Up to max_decisions//2 candidate times plus seeded non-candidate times, in-session only.

    Also returns the candidates of a sequential run (which uses the regime cache)
    so they can be compared with pointwise evaluation.
    """
    rng = random.Random(f"{seed}:{symbol}")
    session = _session_times(frames, symbol, start, end)
    sequential, _ = generate_candidates(symbol, frames, policy, cost, start=start, end=end)
    by_time = {c.decision_time: c for c in sequential}
    cand_times = [t for t in session if t in by_time]
    other = [t for t in session if t not in by_time]
    n_cand = min(len(cand_times), max_decisions // 2)
    picked = rng.sample(cand_times, n_cand)
    picked += rng.sample(other, min(len(other), max_decisions - n_cand))
    return {"times": sorted(picked), "sequential": by_time}


# ----------------------------------------------------------------------
# (a) truncation and (b) future corruption
# ----------------------------------------------------------------------
def _compare(label: str, times: Sequence[pd.Timestamp], baseline: Mapping[pd.Timestamp, dict],
             altered: Callable[[pd.Timestamp], dict]) -> dict[str, Any]:
    mismatches = []
    for t in times:
        got = altered(t)
        if got != baseline[t]:
            mismatches.append({"time": t.isoformat(), "baseline": baseline[t], "altered": got})
    return {"status": "PASS" if not mismatches else "FAIL", "checked": len(times),
            "mismatches": len(mismatches), "examples": mismatches[:MAX_LISTED], "test": label}


def _baseline(frames, policy, cost, symbol, times, decide) -> dict[pd.Timestamp, dict]:
    return {t: _safe(decide, frames, policy, cost, symbol, t) for t in times}


def truncation_test(frames: Frames, policy: Mapping[str, object], cost: CostModel, symbol: str,
                    times: Sequence[pd.Timestamp], *, decide: Decide = replay_decision,
                    baseline: Mapping[pd.Timestamp, dict] | None = None) -> dict[str, Any]:
    baseline = baseline if baseline is not None else _baseline(frames, policy, cost, symbol, times, decide)
    return _compare("truncation", times, baseline,
                    lambda t: _safe(decide, truncate_frames(frames, t), policy, cost, symbol, t))


def future_corruption_test(frames: Frames, policy: Mapping[str, object], cost: CostModel, symbol: str,
                           times: Sequence[pd.Timestamp], *, mode: str = "random", seed: int = 7,
                           decide: Decide = replay_decision,
                           baseline: Mapping[pd.Timestamp, dict] | None = None) -> dict[str, Any]:
    baseline = baseline if baseline is not None else _baseline(frames, policy, cost, symbol, times, decide)
    out = _compare(f"future_corruption:{mode}", times, baseline,
                   lambda t: _safe(decide, corrupt_frames(frames, t, mode, seed), policy, cost, symbol, t))
    out["mode"] = mode
    return out


# ----------------------------------------------------------------------
# (c1) warm-up parity
# ----------------------------------------------------------------------
def observe_fetch_lengths(frames: Frames, policy: Mapping[str, object], cost: CostModel, symbol: str,
                          t: pd.Timestamp, *, lookback_override: int | None = None) -> list[dict[str, Any]]:
    """Record every fetch_ohlc request the regime and technical agents make at t."""
    feed = PointInTimeFeed(frames)
    feed.lookback_override = lookback_override
    feed.set_time(t)
    records: list[dict[str, Any]] = []

    def recording_fetch(sym: str, timeframe: int, num_candles: int) -> pd.DataFrame:
        df = feed.fetch_ohlc(sym, timeframe, num_candles)
        tf = TIMEFRAME_NAMES.get(int(timeframe), str(timeframe))
        records.append({"symbol": sym, "tf": tf, "requested": int(num_candles), "served": len(df),
                        "closed": feed.closed_count(sym, tf) if tf in TIMEFRAME_SECONDS else 0})
        return df

    spread_fn = lambda s: _bar_spread_price(frames, feed, s, cost)
    regime = RegimeAgent(symbol, recording_fetch, policy=policy).evaluate(TIMEFRAME_CODES["H1"])
    if regime.regime not in {"TRENDING_BULL", "TRENDING_BEAR"}:
        # Only the fetch lengths matter here; force a trend so the technical agent fetches its frames.
        regime = replace(regime, regime="TRENDING_BULL", trend_state="BULLISH")
    TechnicalAgent(symbol, recording_fetch, spread_fn, policy=policy).evaluate(
        regime, TIMEFRAME_CODES["M15"], TIMEFRAME_CODES["H1"])
    return records


def warmup_parity(frames: Frames, policy: Mapping[str, object], cost: CostModel, symbol: str,
                  t: pd.Timestamp, *, lookback_override: int | None = None) -> dict[str, Any]:
    records = observe_fetch_lengths(frames, policy, cost, symbol, t, lookback_override=lookback_override)
    bad = [r for r in records if r["served"] != min(r["requested"], r["closed"])]
    live: dict[str, int] = {}
    for r in records:
        live[r["tf"]] = min(live.get(r["tf"], r["requested"]), r["requested"])
    reasons = []
    if lookback_override is not None:
        reasons.append(f"lookback_override={lookback_override} set; replay windows differ from live")
    if bad:
        reasons.append(f"{len(bad)} fetches served a window other than min(requested, closed)")
    if not records:
        reasons.append("no fetches observed")
    return {"status": "FAIL" if reasons else "PASS", "symbol": symbol, "time": pd.Timestamp(t).isoformat(),
            "live_fetch_bars": live, "records": records, "reasons": reasons}


# ----------------------------------------------------------------------
# (c2) warm-up deviation
# ----------------------------------------------------------------------
def _indicator_specs(policy: Mapping[str, object]) -> dict[str, list[tuple[str, str, Callable]]]:
    thresholds = policy.get("AGENT_THRESHOLDS", {})
    reg, tech = thresholds.get("REGIME", {}), thresholds.get("TECHNICAL", {})
    r_fast, r_slow, adx_p = int(reg["ema_fast"]), int(reg["ema_slow"]), int(reg["adx_period"])
    t_fast, t_slow, rsi_p = int(tech["ema_fast"]), int(tech["ema_slow"]), int(tech["rsi_period"])
    return {
        "H1": [(f"ema_{r_fast}", "ema", lambda df: calculate_ema(df["close"], r_fast)),
               (f"ema_{r_slow}", "ema", lambda df: calculate_ema(df["close"], r_slow)),
               (f"adx_{adx_p}", "adx", lambda df: calculate_adx(df, adx_p))],
        "H4": [(f"ema_{t_fast}", "ema", lambda df: calculate_ema(df["close"], t_fast)),
               (f"ema_{t_slow}", "ema", lambda df: calculate_ema(df["close"], t_slow))],
        "M15": [(f"ema_{t_fast}", "ema", lambda df: calculate_ema(df["close"], t_fast)),
                (f"rsi_{rsi_p}", "rsi", lambda df: calculate_rsi(df["close"], rsi_p))],
    }


def synthetic_frame(df: pd.DataFrame, tf: str, bars: int, seed: int) -> pd.DataFrame:
    """Seeded GBM bars with the frame's log-return volatility, starting at its last close."""
    rng = np.random.default_rng(seed)
    closes = df["close"].to_numpy(dtype=float)
    sigma = float(np.nanstd(np.diff(np.log(closes)))) if len(closes) > 2 else 1e-3
    sigma = sigma if sigma > 0 else 1e-4
    close = closes[-1] * np.exp(np.cumsum(rng.normal(0.0, sigma, size=bars)))
    open_ = np.concatenate([[closes[-1]], close[:-1]])
    wick = np.abs(rng.normal(0.0, sigma, size=(2, bars))) * close
    idx = pd.date_range(df.index[-1] + pd.Timedelta(seconds=TIMEFRAME_SECONDS[tf]), periods=bars,
                        freq=f"{TIMEFRAME_SECONDS[tf]}s", tz="UTC")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + wick[0],
                         "low": np.minimum(open_, close) - wick[1], "close": close,
                         "volume": 1.0, "spread_points": 0.0}, index=idx)


def _frame_deviation(symbol: str, tf: str, df: pd.DataFrame, specs, *, long_bars: int, eval_points: int,
                     grid: Sequence[int], thresholds: Mapping[str, float], live_bars: int | None,
                     source: str) -> dict[str, Any]:
    n = len(df)
    first = max(long_bars - 1, n - eval_points)
    entry: dict[str, Any] = {"symbol": symbol, "tf": tf, "source": source, "bars": n,
                             "live_fetch_bars": live_bars}
    if n < long_bars:
        entry.update(status="INCONCLUSIVE", reason=f"{n} bars < long_bars={long_bars}")
        return entry
    points = list(range(first, n))
    lookbacks = sorted(L for L in {*grid, *([live_bars] if live_bars else [])} if L <= min(long_bars, first + 1))
    unit = pip_size(symbol)
    indicators: dict[str, Any] = {}
    ok = True
    for name, kind, fn in specs:
        full = fn(df).to_numpy()
        scale = unit if kind == "ema" else 1.0
        threshold = float(thresholds[kind])
        p95, mx = {}, {}
        for L in lookbacks:
            dev = np.array([abs(fn(df.iloc[i + 1 - L:i + 1]).iat[-1] - full[i]) / scale for i in points])
            dev = np.where(np.isnan(dev), np.inf, dev)  # NaN on the short window = not warmed up
            p95[L] = round(float(np.percentile(dev, 95)), 6)
            mx[L] = round(float(dev.max()), 6)
        required = next((L for L in lookbacks if L in grid and p95[L] < threshold), f">{max(grid)}")
        at_live = None
        if live_bars:
            at_live = {"bars": live_bars, "p95": p95[live_bars], "pass": p95[live_bars] < threshold}
            ok &= at_live["pass"]
        indicators[name] = {"unit": "pips" if kind == "ema" else "points", "threshold": threshold,
                            "p95": p95, "max": mx, "required_warmup": required, "at_live_fetch": at_live}
    entry.update(status="PASS" if ok else "FAIL", eval_points=len(points), indicators=indicators)
    return entry


def warmup_deviation(frames: Frames, policy: Mapping[str, object], *, live_fetch_bars: Mapping[str, int] | None = None,
                     long_bars: int = 2000, eval_points: int = 200, grid: Sequence[int] = DEFAULT_GRID,
                     thresholds: Mapping[str, float] = DEFAULT_THRESHOLDS, synthetic_long_bars: int | None = None,
                     seed: int = 7) -> dict[str, Any]:
    """Recursive-indicator deviation of a short window vs all history, per symbol and timeframe.

    ``live_fetch_bars`` comes from warmup_parity (requested bars per timeframe);
    a real frame passes when every indicator's p95 deviation at that length is
    under its threshold. Synthetic frames are model estimates and never decide
    the status.
    """
    specs = _indicator_specs(policy)
    live = dict(live_fetch_bars or {})
    real, synthetic = [], []
    for s_i, symbol in enumerate(s for s in SYMBOLS if s in frames):
        for t_i, (tf, tf_specs) in enumerate(specs.items()):
            df = frames[symbol].get(tf)
            if df is None or df.empty:
                continue
            kwargs = dict(long_bars=long_bars, eval_points=eval_points, grid=grid, thresholds=thresholds,
                          live_bars=live.get(tf))
            real.append(_frame_deviation(symbol, tf, df, tf_specs, source="real", **kwargs))
            if synthetic_long_bars:
                syn = synthetic_frame(df, tf, int(synthetic_long_bars), seed=seed * 1000 + s_i * 10 + t_i)
                entry = _frame_deviation(symbol, tf, syn, tf_specs, source="model_estimate", **kwargs)
                entry["note"] = "seeded GBM with the frame's log-return volatility; model estimate, not evidence"
                synthetic.append(entry)
    statuses = [e["status"] for e in real]
    if "FAIL" in statuses:
        status = "FAIL"
    elif "PASS" in statuses:
        status = "PASS"
    else:
        status = "INCONCLUSIVE"
    return {"status": status, "long_bars": long_bars, "eval_points": eval_points, "grid": list(grid),
            "thresholds": dict(thresholds), "live_fetch_bars": live, "frames": real, "synthetic": synthetic,
            "synthetic_long_bars": synthetic_long_bars, "seed": seed}


def decision_flips(frames: Frames, policy: Mapping[str, object], cost: CostModel, symbol: str,
                   times: Sequence[pd.Timestamp], *, baseline: Mapping[pd.Timestamp, dict] | None = None,
                   decide: Decide = replay_decision) -> dict[str, Any]:
    """Decisions at the live fetch length vs a lookback of all available bars (reported, not blocking)."""
    override = max(len(df) for df in frames[symbol].values())
    baseline = baseline if baseline is not None else _baseline(frames, policy, cost, symbol, times, decide)
    flips = []
    for t in times:
        long_fp = _safe(decide, frames, policy, cost, symbol, t, lookback_override=override)
        if long_fp != baseline[t]:
            flips.append({"time": t.isoformat(), "live_length": baseline[t], "long_lookback": long_fp})
    return {"status": "REPORTED", "symbol": symbol, "lookback_override": override, "checked": len(times),
            "flips": len(flips), "examples": flips[:MAX_LISTED]}


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------
def _merge(sections: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "PASS" if all(s["status"] == "PASS" for s in sections) else "FAIL",
            "checked": sum(s["checked"] for s in sections), "mismatches": sum(s["mismatches"] for s in sections),
            "by_symbol": list(sections)}


def run_bias_audit(frames: Frames, policy: Mapping[str, object], cost: CostModel = COST_SCENARIOS["base"], *,
                   start=None, end=None, max_decisions: int = 60, seed: int = 7, long_bars: int = 2000,
                   eval_points: int = 200, synthetic_long_bars: int | None = None,
                   extra_times: Mapping[str, Sequence[pd.Timestamp]] | None = None) -> dict[str, Any]:
    symbols = [s for s in SYMBOLS if s in frames]
    trunc, corrupt_random, corrupt_nan, flips, parity, seq_checks = [], [], [], [], [], []
    checked: dict[str, list[str]] = {}
    for symbol in symbols:
        sample = sample_decision_times(frames, policy, cost, symbol, start=start, end=end,
                                       max_decisions=max_decisions, seed=seed)
        times = sorted(set(sample["times"]) | set(pd.Timestamp(t) for t in (extra_times or {}).get(symbol, [])))
        checked[symbol] = [t.isoformat() for t in times]
        if not times:
            continue
        base = _baseline(frames, policy, cost, symbol, times, replay_decision)
        trunc.append({**truncation_test(frames, policy, cost, symbol, times, baseline=base), "symbol": symbol})
        corrupt_random.append({**future_corruption_test(frames, policy, cost, symbol, times, mode="random",
                                                        seed=seed, baseline=base), "symbol": symbol})
        corrupt_nan.append({**future_corruption_test(frames, policy, cost, symbol, times, mode="nan",
                                                     seed=seed, baseline=base), "symbol": symbol})
        flips.append(decision_flips(frames, policy, cost, symbol, times, baseline=base))
        seq_bad = []
        for t, cand in sample["sequential"].items():
            if t in base:
                seq_fp = decision_fingerprint(cand, Counter({"CANDIDATE": 1}))
                point = {k: v for k, v in base[t].items() if k != "stage2"}
                if seq_fp != point:
                    seq_bad.append(t.isoformat())
        seq_checks.append({"symbol": symbol, "status": "PASS" if not seq_bad else "FAIL",
                           "mismatches": seq_bad[:MAX_LISTED]})
        cand_times = [t for t in times if base[t].get("signal")]
        parity.append(warmup_parity(frames, policy, cost, symbol, (cand_times or times)[-1]))

    truncation = _merge(trunc) if trunc else {"status": "FAIL", "reason": "no decision times"}
    corruption = {"status": "PASS" if trunc and all(s["status"] == "PASS" for s in corrupt_random + corrupt_nan)
                  else "FAIL", "random": _merge(corrupt_random) if trunc else {},
                  "nan": _merge(corrupt_nan) if trunc else {}}
    parity_section = {"status": "PASS" if parity and all(p["status"] == "PASS" for p in parity) else "FAIL",
                      "by_symbol": parity}
    live: dict[str, int] = {}
    for p in parity:
        for tf, n in p["live_fetch_bars"].items():
            live[tf] = min(live.get(tf, n), n)
    parity_section["live_fetch_bars"] = live
    deviation = warmup_deviation(frames, policy, live_fetch_bars=live, long_bars=long_bars,
                                 eval_points=eval_points, synthetic_long_bars=synthetic_long_bars, seed=seed)
    core_ok = all(x["status"] == "PASS" for x in (truncation, corruption, parity_section))
    if not core_ok or deviation["status"] == "FAIL":
        verdict = "FAIL"
    elif deviation["status"] == "PASS":
        verdict = "PASS"
    else:
        verdict = "INCONCLUSIVE"
    return {
        "tool": "r1_bias_audit", "verdict": verdict, "truncation": truncation, "future_corruption": corruption,
        "warmup_parity": parity_section, "warmup_deviation": deviation,
        "decision_flips": {"status": "REPORTED", "by_symbol": flips},
        "sequential_vs_pointwise": {"status": "PASS" if all(c["status"] == "PASS" for c in seq_checks) else "FAIL",
                                    "by_symbol": seq_checks, "blocking": False},
        "decision_times_checked": checked, "policy_hash": canonical_policy_hash(policy), "seed": seed,
        "cost": cost.name,
    }


def summary_lines(report: Mapping[str, Any]) -> list[str]:
    tr, fc, wp, wd = (report[k] for k in ("truncation", "future_corruption", "warmup_parity", "warmup_deviation"))
    lines = [
        f"R1 bias audit verdict: {report['verdict']}",
        f"  (a) truncation:          {tr['status']} ({tr.get('checked', 0)} decisions, {tr.get('mismatches', 0)} mismatches)",
        f"  (b) future corruption:   {fc['status']} (random {fc['random'].get('mismatches', '-')}, nan {fc['nan'].get('mismatches', '-')} mismatches)",
        f"  (c1) warm-up parity:     {wp['status']} (live fetch bars {wp['live_fetch_bars']})",
        f"  (c2) warm-up deviation:  {wd['status']} (real frames; long history {wd['long_bars']} bars)",
    ]
    for e in wd["frames"]:
        if e["status"] == "INCONCLUSIVE":
            lines.append(f"       real {e['symbol']} {e['tf']}: INCONCLUSIVE ({e['reason']})")
        else:
            req = ", ".join(f"{k} needs {v['required_warmup']}" for k, v in e["indicators"].items())
            lines.append(f"       real {e['symbol']} {e['tf']}: {e['status']} ({req})")
    for e in wd["synthetic"]:
        if e["status"] == "INCONCLUSIVE":
            continue
        req = ", ".join(f"{k} p95@{e['live_fetch_bars']}={v['at_live_fetch']['p95'] if v['at_live_fetch'] else '-'} "
                        f"needs {v['required_warmup']}" for k, v in e["indicators"].items())
        lines.append(f"       model_estimate {e['symbol']} {e['tf']}: {req}")
    flips = sum(f["flips"] for f in report["decision_flips"]["by_symbol"])
    checked = sum(f["checked"] for f in report["decision_flips"]["by_symbol"])
    lines.append(f"  decision flips at full lookback (reported): {flips}/{checked}")
    lines.append(f"  sequential vs pointwise candidates: {report['sequential_vs_pointwise']['status']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R1 lookahead and recursive-bias audit (offline, read-only)")
    add_source_args(parser)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--max-decisions", type=int, default=60, help="decision times per symbol")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--long-bars", type=int, default=2000)
    parser.add_argument("--eval-points", type=int, default=200)
    parser.add_argument("--synthetic-long-bars", type=int, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    frames = load_frames(bridge_dir=args.bridge_dir, data_dir=args.data_dir, symbols=args.symbols)
    policy, _ = policy_for_variant(args.variant)
    report = run_bias_audit(frames, policy, COST_SCENARIOS["base"], start=parse_ts(args.start), end=parse_ts(args.end),
                            max_decisions=args.max_decisions, seed=args.seed, long_bars=args.long_bars,
                            eval_points=args.eval_points, synthetic_long_bars=args.synthetic_long_bars)
    report.update(variant=args.variant, source=str(source_dir(args)))
    out = args.out or ARTIFACTS / "r1" / run_id()
    write_json(Path(out) / "r1_report.json", report)
    print("\n".join(summary_lines(report)))
    print(f"Report: {Path(out) / 'r1_report.json'}")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
