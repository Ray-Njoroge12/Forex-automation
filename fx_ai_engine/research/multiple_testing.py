"""R3: multiple-testing correction across every variant in the experiment registry.

Offline and read-only. Reads ``artifacts/experiments/registry.jsonl`` (rows written by
``backtesting/run_replay.py``) and, for each row, the replay JSON and ``*_trades.csv``
next to its ``report``. It never runs a replay and never writes next to its inputs.

Per registry row (development trades only; the holdout stays untouched):
  * deflated Sharpe ratio (Bailey and Lopez de Prado 2014) on per-trade R multiples,
    with the trial count N taken from the whole registry and the cross-trial variance of
    Sharpe taken from the row's comparable group (same halts mode and cost profile);
  * probability of backtest overfitting (Bailey, Borwein, Lopez de Prado and Zhu 2017)
    by combinatorially symmetric cross-validation (CSCV) on weekly R sums per group;
  * a minimum dev sample derived from the plan's target expectancy (MIN_TRADES).
Missing or inconsistent inputs give INCONCLUSIVE, never PASS. See
docs/decisions/0006-validation-wave2-multiple-testing.md.

Usage (from fx_ai_engine/):
    USE_MT5_MOCK=1 ../.venv/bin/python -m research.multiple_testing \
        --registry artifacts/experiments/registry.jsonl --out artifacts/research/r3/<run>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy import stats

from backtesting.unified_replay import EXIT_MODES
from config_microcapital import get_policy_config
from research.common import ARTIFACTS, PROJECT_ROOT, run_id, write_json

TARGET_EXPECTANCY_R = 0.25  # plan D2: target dev expectancy per trade, in R
DSR_MIN = 0.95
PBO_MAX = 0.20
CSCV_BLOCKS = 16
CSCV_MIN_WEEKS_PER_BLOCK = 4
POWER = 0.80
ALPHA = 0.05
SD_EPS = 1e-9
FX_WEEK_SHIFT = pd.Timedelta(hours=7)  # FX week opens Sunday 17:00 New York; +7h moves it to Monday 00:00
NEW_YORK = "America/New_York"
DEFAULT_REGISTRY = PROJECT_ROOT / "artifacts" / "experiments" / "registry.jsonl"

PASS_LINE = "# Multiple-testing check (R3): PASS"
NOT_EVIDENCE_LINE = "# NOT EVIDENCE: no variant passed the multiple-testing check (R3)"

SERIES_REASONS = ("SERIES_FILES_MISSING", "SERIES_UNREADABLE", "SERIES_RUN_MISMATCH", "SERIES_COUNT_MISMATCH")
GROUP_REASONS = ("GROUP_TOO_FEW_SERIES", "CSCV_TOO_SHORT", "CSCV_GRID_MISMATCH")
CHECK_ORDER = SERIES_REASONS + ("BELOW_MIN_SAMPLE", "DSR_UNDEFINED", "PSR_BELOW_MIN") + GROUP_REASONS + (
    "GROUP_SERIES_INCOMPLETE", "DSR_BELOW_MIN", "PBO_ABOVE_MAX")
FAIL_REASONS = {"PSR_BELOW_MIN", "DSR_BELOW_MIN", "PBO_ABOVE_MAX"}

NOTES = [
    "The registry rows carry no per-trade series, standard deviation, skew or kurtosis. Sharpe, skew and kurtosis "
    "come from the `*_trades.csv` next to each row's report, and only when that file belongs to the same run "
    "(JSON `generated_at` = `run_id`, same policy_hash and cost) and its dev/holdout counts equal the registry's.",
    "A trial is a distinct (policy_hash, exit_mode) pair over all parseable registry rows, whatever the cost, halts "
    "mode or superseded flag. Rows without a usable series still count toward N.",
    "Cross-trial statistics (variance of Sharpe, CSCV) are computed per group (halts mode, cost profile): SRS-halt "
    "runs, edge runs and different cost profiles are not comparable return streams.",
    "Development trades only (entry_time < holdout_start); the holdout is not used. Unit: r_multiple per trade.",
    "A PASS here is necessary, not sufficient: the replay still needs an R2 authenticity-gate PASS with matching hashes.",
]


# ----------------------------------------------------------------------
# Statistics core
# ----------------------------------------------------------------------

def _finite(x: Any) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clean(obj: Any) -> Any:
    """JSON-safe copy: numpy scalars to Python, non-finite floats to None."""
    if isinstance(obj, Mapping):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def min_sample_design(target_r: float, reward_r: float, alpha: float, power: float) -> dict[str, float | int]:
    """Dev trades needed for a two-sided (1 - alpha) CI to exclude 0 with ``power`` when the mean is ``target_r``.

    Binary trade model: +reward_r with probability q, else -1R, with q chosen so the mean is target_r.
    """
    q = (target_r + 1.0) / (reward_r + 1.0)
    sigma = (reward_r + 1.0) * math.sqrt(q * (1.0 - q))
    z_a = float(stats.norm.ppf(1.0 - alpha / 2.0))
    z_b = float(stats.norm.ppf(power))
    n_exact = ((z_a + z_b) * sigma / target_r) ** 2
    n_ci = (z_a * sigma / target_r) ** 2
    return {"target_r": target_r, "reward_r": reward_r, "alpha": alpha, "power": power, "q": q, "sigma": sigma,
            "z_alpha": z_a, "z_power": z_b, "n_exact": n_exact, "n_min": int(math.ceil(n_exact)),
            "n_ci_only": int(math.ceil(n_ci))}


MIN_SAMPLE = min_sample_design(TARGET_EXPECTANCY_R,
                               float(get_policy_config(mode_id="core_srs", env={})["MIN_RISK_REWARD"]), ALPHA, POWER)
MIN_TRADES = int(MIN_SAMPLE["n_min"])


def sharpe_stats(r: Sequence[float]) -> dict[str, float | int | None]:
    """Per-trade Sharpe (ddof=1), skew and Pearson kurtosis (both biased estimators)."""
    arr = np.asarray(r, dtype=float)
    n = int(arr.size)
    out: dict[str, float | int | None] = {"sr": None, "skew": None, "kurtosis": None, "n": n}
    if n < 2:
        return out
    sd = float(np.std(arr, ddof=1))
    if not math.isfinite(sd) or sd <= SD_EPS:
        return out
    out["sr"] = _finite(arr.mean() / sd)
    out["skew"] = _finite(stats.skew(arr, bias=True))
    out["kurtosis"] = _finite(stats.kurtosis(arr, fisher=False, bias=True))
    return out


def psr(sr: float | None, sr_star: float | None, n: int, skew: float | None, kurt: float | None) -> float | None:
    """Probabilistic Sharpe ratio P(true SR > sr_star); None when undefined."""
    if None in (sr, sr_star, skew, kurt) or n < 2:
        return None
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2
    if not math.isfinite(denom) or denom <= 0:
        return None
    return _finite(stats.norm.cdf((sr - sr_star) * math.sqrt(n - 1) / math.sqrt(denom)))


def expected_max_sharpe(variance: float | None, n_trials: int) -> float | None:
    """SR0: expected maximum Sharpe of ``n_trials`` zero-skill trials with cross-trial variance ``variance``."""
    if n_trials < 2:
        return 0.0
    v = _finite(variance)
    if v is None or v < 0:
        return None
    g = float(np.euler_gamma)
    z = (1.0 - g) * stats.norm.ppf(1.0 - 1.0 / n_trials) + g * stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return _finite(math.sqrt(v) * z)


def fx_week(entry_times: Sequence[Any]) -> pd.PeriodIndex:
    """FX week (Sunday 17:00 to Sunday 17:00 New York) of each UTC time, as a W-SUN period."""
    ts = pd.DatetimeIndex(pd.to_datetime(list(entry_times), utc=True))
    local = ts.tz_convert(NEW_YORK).tz_localize(None) + FX_WEEK_SHIFT
    return local.to_period("W-SUN")


def weekly_matrix(series_by_label: Mapping[str, tuple[Sequence[Any], Sequence[float]]],
                  start: Any, holdout_start: Any) -> pd.DataFrame:
    """Weeks x series of summed r_multiple (0 for no trades), FX weeks from ``start`` to before ``holdout_start``."""
    start_ts = pd.Timestamp(start)
    start_ts = start_ts.tz_localize("UTC") if start_ts.tzinfo is None else start_ts.tz_convert("UTC")
    end_ts = pd.Timestamp(holdout_start)
    end_ts = end_ts.tz_localize("UTC") if end_ts.tzinfo is None else end_ts.tz_convert("UTC")
    first = fx_week([start_ts])[0]
    last = fx_week([end_ts - pd.Timedelta(microseconds=1)])[0]
    grid = pd.period_range(first, last, freq="W-SUN")
    cols = {}
    for label, (times, r) in series_by_label.items():
        s = pd.Series(np.asarray(r, dtype=float), index=fx_week(times))
        cols[label] = s.groupby(level=0).sum().reindex(grid, fill_value=0.0)
    return pd.DataFrame(cols, index=grid)


def _block_sharpe(mask: np.ndarray, cnt: np.ndarray, s: np.ndarray, ss: np.ndarray) -> np.ndarray:
    n = mask @ cnt
    tot = mask @ s
    mean = tot / n[:, None]
    var = np.clip((mask @ ss - tot * mean) / (n[:, None] - 1.0), 0.0, None)
    sd = np.sqrt(var)
    return np.divide(mean, sd, out=np.zeros_like(mean), where=sd > SD_EPS)


def cscv_pbo(matrix: pd.DataFrame | np.ndarray, blocks: int = CSCV_BLOCKS,
             min_rows_per_block: int = CSCV_MIN_WEEKS_PER_BLOCK) -> dict[str, Any]:
    """Probability of backtest overfitting by CSCV over all C(blocks, blocks/2) IS/OOS splits."""
    values = np.asarray(matrix, dtype=float)
    n_rows, n_series = values.shape if values.ndim == 2 else (0, 0)
    out: dict[str, Any] = {"status": "UNAVAILABLE", "pbo": None, "splits": 0, "n_series": int(n_series),
                           "n_rows": int(n_rows), "blocks": blocks, "reason": None}
    if n_series < 2:
        out["reason"] = "GROUP_TOO_FEW_SERIES"
        return out
    if n_rows < blocks * min_rows_per_block:
        out["reason"] = "CSCV_TOO_SHORT"
        return out
    parts = np.array_split(np.arange(n_rows), blocks)
    cnt = np.array([len(p) for p in parts], dtype=float)
    s = np.stack([values[p].sum(axis=0) for p in parts])
    ss = np.stack([(values[p] ** 2).sum(axis=0) for p in parts])
    combos = np.array(list(combinations(range(blocks), blocks // 2)))
    mask = np.zeros((len(combos), blocks))
    mask[np.arange(len(combos))[:, None], combos] = 1.0
    is_sr = _block_sharpe(mask, cnt, s, ss)
    oos_sr = _block_sharpe(1.0 - mask, cnt, s, ss)
    best = is_sr.argmax(axis=1)  # ties: first column
    ranks = stats.rankdata(oos_sr, method="average", axis=1)
    omega = ranks[np.arange(len(combos)), best] / (n_series + 1.0)
    lam = np.log(omega / (1.0 - omega))
    out.update(status="OK", pbo=float(np.mean(lam <= 0)), splits=int(len(combos)),
               lambda_median=float(np.median(lam)))
    return out


# ----------------------------------------------------------------------
# Registry, series and grouping
# ----------------------------------------------------------------------

def load_registry(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Parseable rows (with ``_line``, 1-based) and the number of lines that failed to parse."""
    rows: list[dict[str, Any]] = []
    bad = 0
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("variant", "cost", "policy_hash")):
            bad += 1
            continue
        rows.append({**row, "_line": i})
    return rows, bad


def parse_label(label: str) -> tuple[str, str, str]:
    """``<variant>__<exit_mode>[__<halts>]`` -> (variant, exit_mode, halts). Unknown shape: whole label, halts ''."""
    parts = label.split("__")
    if len(parts) >= 2 and parts[1] in EXIT_MODES:
        return parts[0], parts[1], "__".join(parts[2:])
    return label, label, ""


def trial_keys(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    trials: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        _, exit_mode, _ = parse_label(row["variant"])
        trials.setdefault((row["policy_hash"], exit_mode), set()).add(row["variant"])
    return [{"policy_hash": ph, "exit_mode": em, "labels": sorted(labels)}
            for (ph, em), labels in sorted(trials.items())]


def select_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Per (label, cost): the last non-superseded row in file order, else the last row."""
    by_key: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        by_key.setdefault((row["variant"], row["cost"]), []).append(row)
    evaluated, superseded = [], []
    for candidates in by_key.values():
        live = [r for r in candidates if not r.get("superseded")]
        chosen = (live or candidates)[-1]
        evaluated.append(dict(chosen))
        superseded += [dict(r) for r in candidates if r is not chosen]
    evaluated.sort(key=lambda r: r["_line"])
    superseded.sort(key=lambda r: r["_line"])
    return evaluated, superseded


def _sha16(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def load_series(row: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """Dev r_multiple series for one registry row, with an integrity status."""
    out: dict[str, Any] = {"status": None, "reason": None, "detail": None, "dev_r": None, "dev_entry_times": None,
                           "dev_start": None, "holdout_start": None, "files": {}}

    def fail(reason: str, detail: str) -> dict[str, Any]:
        out.update(status="INVALID", reason=reason, detail=detail)
        return out

    report = row.get("report")
    if not isinstance(report, str) or not report:
        return fail("SERIES_FILES_MISSING", "registry row has no report path")
    md = Path(root) / report
    json_path = md.with_suffix(".json")
    csv_path = md.with_name(md.stem + "_trades.csv")
    out["files"] = {"json": str(json_path), "csv": str(csv_path)}
    missing = [p.name for p in (json_path, csv_path) if not p.is_file()]
    if missing:
        return fail("SERIES_FILES_MISSING", "missing " + ", ".join(missing))
    try:
        rep = json.loads(json_path.read_text(encoding="utf-8"))
        holdout_start = _utc(rep["holdout_start"])
        dev_start = _utc(str(rep["development"]["range"]).split(" to ")[0])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return fail("SERIES_UNREADABLE", f"{json_path.name}: {exc}")
    mismatched = [k for k, rk in (("generated_at", "run_id"), ("policy_hash", "policy_hash"), ("cost", "cost"))
                  if str(rep.get(k)) != str(row.get(rk))]
    if mismatched:
        return fail("SERIES_RUN_MISMATCH", "replay JSON differs from registry row in " + ", ".join(mismatched))
    try:
        trades = pd.read_csv(csv_path, usecols=["entry_time", "r_multiple"])
        entry = pd.to_datetime(trades["entry_time"], utc=True)
        r = pd.to_numeric(trades["r_multiple"], errors="raise").astype(float)
    except (OSError, ValueError, KeyError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        return fail("SERIES_UNREADABLE", f"{csv_path.name}: {exc}")
    if entry.isna().any() or not np.isfinite(r.to_numpy()).all():
        return fail("SERIES_UNREADABLE", f"{csv_path.name}: missing entry_time or non-finite r_multiple")
    is_dev = (entry < holdout_start).to_numpy()
    n_dev, n_hold = int(is_dev.sum()), int((~is_dev).sum())
    want_dev = (row.get("dev") or {}).get("trades")
    want_hold = (row.get("holdout") or {}).get("trades")
    if n_dev != want_dev or n_hold != want_hold:
        return fail("SERIES_COUNT_MISMATCH",
                    f"trades file has dev {n_dev} / holdout {n_hold}, registry says {want_dev} / {want_hold}")
    out["files"] = {"json": str(json_path), "json_sha256_16": _sha16(json_path),
                    "csv": str(csv_path), "csv_sha256_16": _sha16(csv_path)}
    out.update(status="OK", dev_r=r.to_numpy()[is_dev], dev_entry_times=list(entry[is_dev]),
               dev_start=dev_start, holdout_start=holdout_start)
    return out


# ----------------------------------------------------------------------
# Verdict
# ----------------------------------------------------------------------

def _group_stats(key: tuple[str, str], members: Sequence[dict[str, Any]], n_trials: int) -> dict[str, Any]:
    halts, cost = key
    valid = [m for m in members if m["series"]["status"] == "OK"]
    srs = [m["stats"]["sr"] for m in valid if m["stats"]["sr"] is not None]
    variance = float(np.var(srs, ddof=1)) if len(srs) >= 2 else None
    sr0 = 0.0 if n_trials < 2 else expected_max_sharpe(variance, n_trials)
    if len(valid) < 2:
        cscv = cscv_pbo(np.zeros((0, len(valid))))
        cscv["n_series"] = len(valid)
    elif len({(m["series"]["dev_start"], m["series"]["holdout_start"]) for m in valid}) > 1:
        cscv = {"status": "UNAVAILABLE", "pbo": None, "splits": 0, "n_series": len(valid), "n_rows": 0,
                "blocks": CSCV_BLOCKS, "reason": "CSCV_GRID_MISMATCH"}
    else:
        ser = valid[0]["series"]
        matrix = weekly_matrix({m["row"]["variant"]: (m["series"]["dev_entry_times"], m["series"]["dev_r"])
                                for m in valid}, ser["dev_start"], ser["holdout_start"])
        cscv = cscv_pbo(matrix)
        cscv["grid"] = f"{matrix.index[0]} .. {matrix.index[-1]}"
    status = "OK" if cscv["status"] == "OK" else cscv["reason"]
    return {"halts": halts, "cost": cost, "labels": [m["row"]["variant"] for m in members],
            "series_labels": [m["row"]["variant"] for m in valid],
            "missing_labels": [m["row"]["variant"] for m in members if m["series"]["status"] != "OK"],
            "sr_values": srs, "sr_variance": variance, "sr0": sr0, "status": status, "cscv": cscv}


def _row_reason(m: Mapping[str, Any], group: Mapping[str, Any]) -> str | None:
    """First failing check in CHECK_ORDER, or None for PASS."""
    if m["series"]["status"] != "OK":
        return m["series"]["reason"]
    if m["stats"]["n"] < MIN_TRADES:
        return "BELOW_MIN_SAMPLE"
    if m["psr0"] is None:
        return "DSR_UNDEFINED"
    if m["psr0"] < DSR_MIN:
        return "PSR_BELOW_MIN"
    if group["status"] != "OK":
        return group["status"]
    if group["missing_labels"]:
        return "GROUP_SERIES_INCOMPLETE"
    if m["dsr"] is None:
        return "DSR_UNDEFINED"
    if m["dsr"] < DSR_MIN:
        return "DSR_BELOW_MIN"
    if group["cscv"]["pbo"] > PBO_MAX:
        return "PBO_ABOVE_MAX"
    return None


def evaluate(registry_path: Path, root: Path) -> dict[str, Any]:
    registry_path, root = Path(registry_path), Path(root)
    reasons: list[str] = []
    if registry_path.is_file():
        rows, bad = load_registry(registry_path)
        registry_sha = _sha16(registry_path)
    else:
        rows, bad, registry_sha = [], 0, None
        reasons.append("REGISTRY_MISSING")
    if not rows and registry_path.is_file():
        reasons.append("REGISTRY_EMPTY")
    if bad:
        reasons.append(f"REGISTRY_LINE_UNPARSEABLE: {bad} line(s)")
    trials = trial_keys(rows)
    n_trials = len(trials)
    evaluated, superseded = select_rows(rows)

    members = []
    for row in evaluated:
        variant, exit_mode, halts = parse_label(row["variant"])
        series = load_series(row, root)
        st = sharpe_stats(series["dev_r"]) if series["status"] == "OK" else {"sr": None, "skew": None,
                                                                             "kurtosis": None, "n": 0}
        members.append({"row": row, "variant_base": variant, "exit_mode": exit_mode, "halts": halts,
                        "series": series, "stats": st,
                        "psr0": psr(st["sr"], 0.0, st["n"], st["skew"], st["kurtosis"]),
                        "expectancy_r": _finite(np.mean(series["dev_r"])) if series["status"] == "OK" and st["n"] else None})

    by_group: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for m in members:
        by_group.setdefault((m["halts"], m["row"]["cost"]), []).append(m)
    groups = {key: _group_stats(key, ms, n_trials) for key, ms in sorted(by_group.items())}

    variants = []
    for m in members:
        g = groups[(m["halts"], m["row"]["cost"])]
        st = m["stats"]
        m["dsr"] = psr(st["sr"], g["sr0"], st["n"], st["skew"], st["kurtosis"])
        reason = _row_reason(m, g)
        verdict = "PASS" if reason is None else ("FAIL" if reason in FAIL_REASONS else "INCONCLUSIVE")
        row = m["row"]
        variants.append({
            "label": row["variant"], "cost": row["cost"], "run_id": row.get("run_id"),
            "policy_hash": row["policy_hash"], "variant": m["variant_base"], "exit_mode": m["exit_mode"],
            "halts": m["halts"], "registry_line": row["_line"], "series_status": m["series"]["status"],
            "series_detail": m["series"]["detail"], "files": m["series"]["files"],
            "dev_trades": st["n"] if m["series"]["status"] == "OK" else (row.get("dev") or {}).get("trades"),
            "expectancy_r": m["expectancy_r"], "sharpe_per_trade": st["sr"], "skew": st["skew"],
            "kurtosis": st["kurtosis"], "psr0": m["psr0"], "sr0": g["sr0"], "dsr": m["dsr"],
            "pbo": g["cscv"]["pbo"], "verdict": verdict,
            "evidence_status": "EVIDENCE_ELIGIBLE" if verdict == "PASS" else "NOT_EVIDENCE",
            "reasons": [] if reason is None else [reason]})

    registry_blocked = any(r.split(":")[0] in {"REGISTRY_MISSING", "REGISTRY_EMPTY", "REGISTRY_LINE_UNPARSEABLE"}
                           for r in reasons)
    verdicts = {v["verdict"] for v in variants}
    if registry_blocked:
        overall = "INCONCLUSIVE"
    elif "PASS" in verdicts:
        overall = "PASS"
    elif "INCONCLUSIVE" in verdicts:
        overall = "INCONCLUSIVE"
    else:
        overall = "FAIL"
    counts = Counter(v["reasons"][0] if v["reasons"] else "PASS" for v in variants)
    reasons += [f"{code}: {counts[code]} row(s)" for code in CHECK_ORDER if counts.get(code)]

    missing_inputs = [{"label": v["label"], "cost": v["cost"], "run_id": v["run_id"],
                       "reason": v["reasons"][0], "detail": v["series_detail"]}
                      for v in variants if v["series_status"] != "OK"]
    group_out = []
    for g in groups.values():
        group_out.append({k: g[k] for k in ("halts", "cost", "labels", "series_labels", "missing_labels",
                                            "sr_variance", "sr0", "status", "cscv")})
    return _clean({
        "tool": "r3_multiple_testing", "verdict": overall,
        "evidence_status": "EVIDENCE_ELIGIBLE" if overall == "PASS" else "NOT_EVIDENCE",
        "registry": str(registry_path), "registry_sha256_16": registry_sha, "root": str(root),
        "rows_read": len(rows), "unparseable_lines": bad, "rows_evaluated": len(evaluated),
        "trials_n": n_trials, "trial_keys": trials,
        "thresholds": {"dsr_min": DSR_MIN, "pbo_max": PBO_MAX, "min_trades": MIN_TRADES,
                       "min_sample_design": MIN_SAMPLE, "cscv_blocks": CSCV_BLOCKS,
                       "min_weeks": CSCV_BLOCKS * CSCV_MIN_WEEKS_PER_BLOCK},
        "reason_counts": dict(counts), "groups": group_out, "variants": variants,
        "superseded_rows": [{"label": r["variant"], "cost": r["cost"], "run_id": r.get("run_id"),
                             "registry_line": r["_line"], "superseded_flag": bool(r.get("superseded"))}
                            for r in superseded],
        "missing_inputs": missing_inputs, "reasons": reasons, "notes": list(NOTES)})


def _fmt(x: Any, digits: int = 4) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def render_markdown(verdict: Mapping[str, Any]) -> str:
    passed = verdict["verdict"] == "PASS"
    th = verdict["thresholds"]
    ms = th["min_sample_design"]
    lines = [PASS_LINE if passed else NOT_EVIDENCE_LINE, "",
             f"Verdict **{verdict['verdict']}**, evidence status **{verdict['evidence_status']}**. "
             f"Registry `{verdict['registry']}` (sha256_16 `{verdict['registry_sha256_16']}`): "
             f"{verdict['rows_read']} rows read, {verdict['unparseable_lines']} unparseable, "
             f"{verdict['rows_evaluated']} evaluated, {len(verdict['superseded_rows'])} superseded.", "",
             f"Trials N = **{verdict['trials_n']}**: distinct (policy_hash, exit_mode) pairs over all parseable rows, "
             "whatever the cost, halts mode or superseded flag. Rows without a usable series still count.", "",
             "## Thresholds", "",
             f"- Deflated Sharpe ratio >= {th['dsr_min']} (and PSR(0) >= {th['dsr_min']}).",
             f"- PBO (CSCV, {th['cscv_blocks']} blocks of FX weeks, needs >= {th['min_weeks']} weeks) <= {th['pbo_max']}.",
             f"- Dev trades >= {th['min_trades']}: target mean {ms['target_r']}R, reward {ms['reward_r']}R vs -1R, "
             f"q = {ms['q']:.6f}, sigma = {ms['sigma']:.4f}, n = (({ms['z_alpha']:.4f} + {ms['z_power']:.4f}) x "
             f"sigma / {ms['target_r']})^2 = {ms['n_exact']:.1f}, rounded up to {ms['n_min']} "
             f"(two-sided {1 - ms['alpha']:.0%} CI excludes 0 with {ms['power']:.0%} power; CI alone would need "
             f"{ms['n_ci_only']}).", "",
             "## Groups (halts mode, cost)", "",
             "| halts | cost | series | missing | weeks | V(SR) | SR0 | PBO | status |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for g in verdict["groups"]:
        c = g["cscv"]
        lines.append(f"| {g['halts'] or '(none)'} | {g['cost']} | {len(g['series_labels'])} | {len(g['missing_labels'])} | "
                     f"{c.get('n_rows') or '-'} | {_fmt(g['sr_variance'], 5)} | {_fmt(g['sr0'])} | {_fmt(c.get('pbo'))} | "
                     f"{g['status']} |")
    lines += ["", "## Rows", "",
              "| label | cost | run_id | dev trades | E[R] | SR/trade | skew | kurt | PSR(0) | DSR | verdict | reason | evidence |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for v in verdict["variants"]:
        evidence = "EVIDENCE_ELIGIBLE" if v["evidence_status"] == "EVIDENCE_ELIGIBLE" else "NOT EVIDENCE"
        lines.append(f"| {v['label']} | {v['cost']} | {v['run_id']} | {_fmt(v['dev_trades'])} | {_fmt(v['expectancy_r'])} | "
                     f"{_fmt(v['sharpe_per_trade'])} | {_fmt(v['skew'], 3)} | {_fmt(v['kurtosis'], 3)} | {_fmt(v['psr0'])} | "
                     f"{_fmt(v['dsr'])} | {v['verdict']} | {', '.join(v['reasons']) or '-'} | {evidence} |")
    lines += ["", "## What is missing", ""]
    if verdict["missing_inputs"]:
        lines += [f"- {m['label']} / {m['cost']} (run {m['run_id']}): {m['reason']}: {m['detail']}"
                  for m in verdict["missing_inputs"]]
    else:
        lines.append("- Every evaluated row has a matching trades series.")
    for g in verdict["groups"]:
        if g["status"] != "OK":
            lines.append(f"- Group {g['halts'] or '(none)'} / {g['cost']}: {g['status']} "
                         f"({len(g['series_labels'])} valid series); V and PBO cannot be computed.")
    reg = [r for r in verdict["reasons"] if r.startswith("REGISTRY_")]
    lines += [f"- {r}" for r in reg]
    if verdict["superseded_rows"]:
        lines += ["", f"Superseded rows not evaluated: {len(verdict['superseded_rows'])} "
                      "(they still count toward N)."]
    lines += ["", "## Notes", ""] + [f"- {n}" for n in verdict["notes"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R3 multiple-testing check over the experiment registry (offline, read-only)")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--root", type=Path, default=None, help="base for registry 'report' paths (default: registry/../../..)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    registry = args.registry.resolve()
    root = (args.root or registry.parents[2]).resolve()
    verdict = evaluate(registry, root)
    out = Path(args.out or ARTIFACTS / "r3" / run_id())
    write_json(out / "verdict.json", verdict)
    (out / "report.md").write_text(render_markdown(verdict), encoding="utf-8")
    print(f"R3 multiple-testing verdict: {verdict['verdict']} ({verdict['evidence_status']})")
    print(f"  trials N={verdict['trials_n']} rows read={verdict['rows_read']} evaluated={verdict['rows_evaluated']} "
          f"superseded={len(verdict['superseded_rows'])} unparseable={verdict['unparseable_lines']} "
          f"min_trades={verdict['thresholds']['min_trades']}")
    for g in verdict["groups"]:
        print(f"  group {g['halts'] or '(none)'}/{g['cost']}: series={len(g['series_labels'])} "
              f"missing={len(g['missing_labels'])} SR0={_fmt(g['sr0'])} PBO={_fmt(g['cscv'].get('pbo'))} {g['status']}")
    for v in verdict["variants"]:
        print(f"  {v['label']:34} {v['cost']:12} n={_fmt(v['dev_trades'])} PSR0={_fmt(v['psr0'])} DSR={_fmt(v['dsr'])} "
              f"{v['verdict']:12} {', '.join(v['reasons'])}")
    for r in verdict["reasons"]:
        print(f"  reason: {r}")
    print(f"Outputs: {out}")
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
