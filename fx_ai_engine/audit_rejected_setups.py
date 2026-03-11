from __future__ import annotations

import argparse
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from backtesting.data_loader import load_ohlc_csv
from core.credentials import CredentialsError, load_mt5_credentials_from_env
from core.env_loader import load_runtime_env
from core.mt5_bridge import MT5Connection
from core.timeframes import TIMEFRAME_M15

DB_PATH = Path(__file__).parent / "database" / "trading_state.db"
AUDITABLE_TECH_REASONS = {
    "TECH_PULLBACK_OR_RSI_INVALID",
    "TECH_CANDLE_CONFIRMATION_FAILED",
    "TECH_REGIME_NOT_TRENDING",
}
DUPLICATE_REJECT_EVENTS = {
    ("TECHNICAL", "TECH_SKIPPED_REGIME_REJECTED"),
}
AUDITABLE_LATER_STAGES = {
    "STRATEGIC_RISK",
    "ADVERSARIAL",
    "PORTFOLIO",
    "HARD_RISK",
    "ML_RANKER",
    "PRE_ROUTE_FEASIBILITY",
    "ROUTER",
}
CSV_CANDIDATE_NAMES = (
    "{symbol}.csv",
    "{symbol_lower}.csv",
    "{symbol}_M15.csv",
    "{symbol_lower}_m15.csv",
    "{symbol}_m15.csv",
)
DETAIL_TOKEN_RE = re.compile(r"([A-Za-z0-9_]+)=([^\s]+)")


@dataclass(frozen=True)
class ScopeFilters:
    days: int
    evidence_stream: str | None = None
    policy_mode: str | None = None
    execution_mode: str | None = "mt5"
    account_scope: str | None = None


@dataclass(frozen=True)
class CounterfactualCandidate:
    decision_time: datetime
    symbol: str
    stage: str
    reason_code: str
    trade_id: str | None
    direction: str
    details: str
    reference_price: float | None
    reference_mode: str
    stop_pips: float | None
    target_pips: float | None


@dataclass(frozen=True)
class CounterfactualResult:
    candidate: CounterfactualCandidate
    outcome_label: str
    bars_observed: int
    horizon_bars: int
    mfe_pips: float
    mae_pips: float
    threshold_hit: bool | None
    threshold_pips: float | None
    resolved_reference_price: float | None
    resolved_reference_mode: str


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _scope_where(
    conn: sqlite3.Connection,
    table: str,
    *,
    filters: ScopeFilters,
    since_column: str,
) -> tuple[str, list[Any]]:
    columns = _column_names(conn, table)
    clauses: list[str] = []
    params: list[Any] = []
    since = (datetime.now(timezone.utc) - timedelta(days=max(int(filters.days), 1))).isoformat()
    if since_column in columns:
        clauses.append(f"{since_column} >= ?")
        params.append(since)
    for column in ("evidence_stream", "policy_mode", "execution_mode", "account_scope"):
        value = getattr(filters, column)
        if value is None or column not in columns:
            continue
        clauses.append(f"{column} = ?")
        params.append(value)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


def _parse_iso8601(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _positive_float(value: Any) -> float | None:
    parsed = _safe_float(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _pip_value(symbol: str) -> float:
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def _floor_to_m15(value: datetime) -> datetime:
    minute = (value.minute // 15) * 15
    return value.replace(minute=minute, second=0, microsecond=0)


def _parse_detail_tokens(details: str) -> dict[str, str]:
    return {key: value.rstrip(",") for key, value in DETAIL_TOKEN_RE.findall(details or "")}


def _load_trade_lookup(conn: sqlite3.Connection, filters: ScopeFilters) -> dict[str, sqlite3.Row]:
    if not _table_exists(conn, "trades"):
        return {}
    where_sql, params = _scope_where(conn, "trades", filters=filters, since_column="open_time")
    rows = conn.execute(
        f"""
        SELECT trade_id, symbol, direction, stop_loss, take_profit, entry_price,
               status, reason_code, open_time
          FROM trades
          {where_sql}
        """,
        params,
    ).fetchall()
    return {str(row["trade_id"]): row for row in rows if row["trade_id"]}


def extract_counterfactual_candidates(
    conn: sqlite3.Connection,
    *,
    filters: ScopeFilters,
) -> dict[str, Any]:
    trade_lookup = _load_trade_lookup(conn, filters)
    candidates: list[CounterfactualCandidate] = []
    gap_counts: Counter[tuple[str, str, str]] = Counter()
    total_reject_like_events = 0

    if _table_exists(conn, "decision_funnel_events"):
        where_sql, params = _scope_where(
            conn,
            "decision_funnel_events",
            filters=filters,
            since_column="decision_time",
        )
        funnel_rows = conn.execute(
            f"""
            SELECT decision_time, symbol, stage, outcome, reason_code, trade_id, details
              FROM decision_funnel_events
              {where_sql}
             ORDER BY id DESC
            """,
            params,
        ).fetchall()
        for row in funnel_rows:
            stage = str(row["stage"] or "")
            outcome = str(row["outcome"] or "")
            reason_code = str(row["reason_code"] or "")
            symbol = str(row["symbol"] or "")
            details = str(row["details"] or "")
            trade_id = str(row["trade_id"]) if row["trade_id"] else None
            if outcome not in {"REJECT", "SKIP"}:
                continue
            if (stage, reason_code) in DUPLICATE_REJECT_EVENTS:
                continue
            total_reject_like_events += 1
            if stage == "REGIME" and outcome == "REJECT":
                detail_tokens = _parse_detail_tokens(details)
                direction = str(detail_tokens.get("direction_candidate") or "").upper()
                reference_price = _positive_float(detail_tokens.get("close"))
                if direction in {"BUY", "SELL"} and reference_price is not None and symbol:
                    candidates.append(
                        CounterfactualCandidate(
                            decision_time=_parse_iso8601(row["decision_time"]),
                            symbol=symbol,
                            stage=stage,
                            reason_code=reason_code,
                            trade_id=trade_id,
                            direction=direction,
                            details=details,
                            reference_price=reference_price,
                            reference_mode="exact_details_close",
                            stop_pips=None,
                            target_pips=None,
                        )
                    )
                else:
                    gap_counts[(stage, reason_code, symbol)] += 1
                continue
            if stage == "TECHNICAL" and outcome == "REJECT" and reason_code in AUDITABLE_TECH_REASONS:
                detail_tokens = _parse_detail_tokens(details)
                direction = str(detail_tokens.get("direction") or "").upper()
                reference_price = _positive_float(detail_tokens.get("close"))
                if direction in {"BUY", "SELL"} and reference_price is not None and symbol:
                    candidates.append(
                        CounterfactualCandidate(
                            decision_time=_parse_iso8601(row["decision_time"]),
                            symbol=symbol,
                            stage=stage,
                            reason_code=reason_code,
                            trade_id=trade_id,
                            direction=direction,
                            details=details,
                            reference_price=reference_price,
                            reference_mode="exact_details_close",
                            stop_pips=None,
                            target_pips=None,
                        )
                    )
                else:
                    gap_counts[(stage, reason_code, symbol)] += 1
                continue
            if outcome == "REJECT" and stage in AUDITABLE_LATER_STAGES and trade_id:
                trade_row = trade_lookup.get(trade_id)
                direction = str(trade_row["direction"] or "").upper() if trade_row else ""
                if trade_row and symbol and direction in {"BUY", "SELL"}:
                    entry_price = _positive_float(trade_row["entry_price"])
                    candidates.append(
                        CounterfactualCandidate(
                            decision_time=_parse_iso8601(row["decision_time"]),
                            symbol=symbol,
                            stage=stage,
                            reason_code=reason_code,
                            trade_id=trade_id,
                            direction=direction,
                            details=details,
                            reference_price=entry_price,
                            reference_mode="trade_entry_price" if entry_price else "decision_bar_open_approx",
                            stop_pips=_positive_float(trade_row["stop_loss"]),
                            target_pips=_positive_float(trade_row["take_profit"]),
                        )
                    )
                else:
                    gap_counts[(stage, reason_code, symbol)] += 1
                continue
            gap_counts[(stage, reason_code, symbol)] += 1

    broker_result_trade_ids: set[str] = set()
    if _table_exists(conn, "trades"):
        where_sql, params = _scope_where(conn, "trades", filters=filters, since_column="open_time")
        rejected_rows = conn.execute(
            f"""
            SELECT trade_id, symbol, direction, stop_loss, take_profit, entry_price,
                   status, reason_code, open_time
              FROM trades
              {where_sql}
               {'AND' if where_sql else 'WHERE'} status = 'REJECTED'
               AND reason_code LIKE 'REJECTED%'
             ORDER BY id DESC
            """,
            params,
        ).fetchall()
        for row in rejected_rows:
            total_reject_like_events += 1
            trade_id = str(row["trade_id"] or "")
            symbol = str(row["symbol"] or "")
            direction = str(row["direction"] or "").upper()
            reason_code = str(row["reason_code"] or "")
            if not trade_id or not symbol or direction not in {"BUY", "SELL"}:
                gap_counts[("BROKER_RESULT", reason_code, symbol)] += 1
                continue
            broker_result_trade_ids.add(trade_id)
            entry_price = _positive_float(row["entry_price"])
            candidates.append(
                CounterfactualCandidate(
                    decision_time=_parse_iso8601(row["open_time"]),
                    symbol=symbol,
                    stage="BROKER_RESULT",
                    reason_code=reason_code,
                    trade_id=trade_id,
                    direction=direction,
                    details=f"status={row['status']}",
                    reference_price=entry_price,
                    reference_mode="trade_entry_price" if entry_price else "decision_bar_open_approx",
                    stop_pips=_positive_float(row["stop_loss"]),
                    target_pips=_positive_float(row["take_profit"]),
                )
            )

    return {
        "total_reject_like_events": total_reject_like_events,
        "total_telemetry_gap_events": sum(gap_counts.values()),
        "candidates": candidates,
        "telemetry_gaps": [
            {
                "stage": stage,
                "reason_code": reason_code,
                "symbol": symbol,
                "count": count,
            }
            for (stage, reason_code, symbol), count in gap_counts.most_common(12)
        ],
        "broker_result_trade_ids": broker_result_trade_ids,
    }


def _resolve_reference_price(
    candidate: CounterfactualCandidate,
    bars: pd.DataFrame,
) -> tuple[float | None, str]:
    if candidate.reference_price is not None and candidate.reference_price > 0:
        return candidate.reference_price, candidate.reference_mode
    if bars.empty:
        return None, candidate.reference_mode
    anchor_time = _floor_to_m15(candidate.decision_time)
    anchor_slice = bars.loc[bars.index <= anchor_time]
    if anchor_slice.empty:
        future_slice = bars.loc[bars.index >= anchor_time]
        if future_slice.empty:
            return None, candidate.reference_mode
        anchor_row = future_slice.iloc[0]
    else:
        anchor_row = anchor_slice.iloc[-1]
    return float(anchor_row["open"]), "decision_bar_open_approx"


def evaluate_candidate_on_bars(
    candidate: CounterfactualCandidate,
    bars: pd.DataFrame,
    *,
    horizon_bars: int,
    threshold_pips: float | None = 10.0,
) -> CounterfactualResult:
    normalized = bars.sort_index() if not bars.empty else bars
    reference_price, reference_mode = _resolve_reference_price(candidate, normalized)
    if reference_price is None or normalized.empty:
        return CounterfactualResult(
            candidate=candidate,
            outcome_label="NO_FORWARD_BARS",
            bars_observed=0,
            horizon_bars=horizon_bars,
            mfe_pips=0.0,
            mae_pips=0.0,
            threshold_hit=None,
            threshold_pips=threshold_pips,
            resolved_reference_price=None,
            resolved_reference_mode=reference_mode,
        )

    anchor_time = _floor_to_m15(candidate.decision_time)
    forward = normalized.loc[normalized.index >= anchor_time].head(max(int(horizon_bars), 1))
    if forward.empty:
        return CounterfactualResult(
            candidate=candidate,
            outcome_label="NO_FORWARD_BARS",
            bars_observed=0,
            horizon_bars=horizon_bars,
            mfe_pips=0.0,
            mae_pips=0.0,
            threshold_hit=None,
            threshold_pips=threshold_pips,
            resolved_reference_price=reference_price,
            resolved_reference_mode=reference_mode,
        )

    pip_value = _pip_value(candidate.symbol)
    direction = candidate.direction
    if direction == "BUY":
        mfe_pips = max(float(row["high"]) - reference_price for _, row in forward.iterrows()) / pip_value
        mae_pips = max(reference_price - float(row["low"]) for _, row in forward.iterrows()) / pip_value
    else:
        mfe_pips = max(reference_price - float(row["low"]) for _, row in forward.iterrows()) / pip_value
        mae_pips = max(float(row["high"]) - reference_price for _, row in forward.iterrows()) / pip_value
    mfe_pips = round(max(mfe_pips, 0.0), 2)
    mae_pips = round(max(mae_pips, 0.0), 2)

    if candidate.stop_pips and candidate.target_pips:
        stop_delta = candidate.stop_pips * pip_value
        target_delta = candidate.target_pips * pip_value
        if direction == "BUY":
            stop_price = reference_price - stop_delta
            target_price = reference_price + target_delta
        else:
            stop_price = reference_price + stop_delta
            target_price = reference_price - target_delta
        label = "TIMEOUT"
        for _, row in forward.iterrows():
            high = float(row["high"])
            low = float(row["low"])
            target_hit = high >= target_price if direction == "BUY" else low <= target_price
            stop_hit = low <= stop_price if direction == "BUY" else high >= stop_price
            if target_hit and stop_hit:
                label = "AMBIGUOUS_BOTH_HIT_SAME_BAR"
                break
            if target_hit:
                label = "TARGET_FIRST"
                break
            if stop_hit:
                label = "STOP_FIRST"
                break
        if label == "TIMEOUT" and len(forward) < horizon_bars:
            label = "INSUFFICIENT_FORWARD_BARS"
        return CounterfactualResult(
            candidate=candidate,
            outcome_label=label,
            bars_observed=len(forward),
            horizon_bars=horizon_bars,
            mfe_pips=mfe_pips,
            mae_pips=mae_pips,
            threshold_hit=None,
            threshold_pips=threshold_pips,
            resolved_reference_price=round(reference_price, 5),
            resolved_reference_mode=reference_mode,
        )

    threshold_hit = None
    if threshold_pips is not None:
        threshold_hit = mfe_pips >= float(threshold_pips)
    return CounterfactualResult(
        candidate=candidate,
        outcome_label="EXCURSION_ONLY",
        bars_observed=len(forward),
        horizon_bars=horizon_bars,
        mfe_pips=mfe_pips,
        mae_pips=mae_pips,
        threshold_hit=threshold_hit,
        threshold_pips=threshold_pips,
        resolved_reference_price=round(reference_price, 5),
        resolved_reference_mode=reference_mode,
    )


def build_rejected_setup_audit(
    conn: sqlite3.Connection,
    *,
    filters: ScopeFilters,
    bars_by_symbol: Mapping[str, pd.DataFrame],
    horizon_bars: int = 16,
    threshold_pips: float | None = 10.0,
) -> dict[str, Any]:
    extracted = extract_counterfactual_candidates(conn, filters=filters)
    candidates: list[CounterfactualCandidate] = extracted["candidates"]
    results = [
        evaluate_candidate_on_bars(
            candidate,
            bars_by_symbol.get(candidate.symbol, pd.DataFrame()),
            horizon_bars=horizon_bars,
            threshold_pips=threshold_pips,
        )
        for candidate in candidates
    ]

    reference_modes = Counter(result.resolved_reference_mode for result in results)
    outcome_counts = Counter(result.outcome_label for result in results)
    threshold_evaluable = [result for result in results if result.threshold_hit is not None]
    threshold_hits = sum(1 for result in threshold_evaluable if result.threshold_hit)

    hotspots: dict[tuple[str, str, str], dict[str, Any]] = {}
    grouped: dict[tuple[str, str, str], list[CounterfactualResult]] = {}
    for result in results:
        key = (
            result.candidate.stage,
            result.candidate.reason_code,
            result.candidate.symbol,
        )
        grouped.setdefault(key, []).append(result)
    for key, rows in grouped.items():
        stage, reason_code, symbol = key
        path_rows = [row for row in rows if row.outcome_label != "EXCURSION_ONLY"]
        threshold_rows = [row for row in rows if row.threshold_hit is not None]
        hotspots[key] = {
            "stage": stage,
            "reason_code": reason_code,
            "symbol": symbol,
            "count": len(rows),
            "avg_mfe_pips": round(sum(row.mfe_pips for row in rows) / len(rows), 2),
            "avg_mae_pips": round(sum(row.mae_pips for row in rows) / len(rows), 2),
            "target_first_rate": (
                sum(1 for row in path_rows if row.outcome_label == "TARGET_FIRST") / len(path_rows)
                if path_rows
                else None
            ),
            "threshold_hit_rate": (
                sum(1 for row in threshold_rows if row.threshold_hit) / len(threshold_rows)
                if threshold_rows
                else None
            ),
            "approximate_reference_share": (
                sum(1 for row in rows if row.resolved_reference_mode == "decision_bar_open_approx") / len(rows)
            ),
        }

    return {
        "summary": {
            "total_reject_like_events": extracted["total_reject_like_events"],
            "audited_candidates": len(candidates),
            "exact_reference_candidates": sum(
                1 for result in results if result.resolved_reference_mode != "decision_bar_open_approx"
            ),
            "approximate_reference_candidates": sum(
                1 for result in results if result.resolved_reference_mode == "decision_bar_open_approx"
            ),
            "path_labeled_candidates": sum(
                1 for result in results if result.outcome_label not in {"EXCURSION_ONLY", "NO_FORWARD_BARS"}
            ),
            "excursion_only_candidates": outcome_counts.get("EXCURSION_ONLY", 0),
            "telemetry_gap_events": extracted["total_telemetry_gap_events"],
            "threshold_evaluable_candidates": len(threshold_evaluable),
            "threshold_hit_candidates": threshold_hits,
        },
        "outcome_summary": [
            {"outcome_label": label, "count": count}
            for label, count in outcome_counts.most_common()
        ],
        "reason_hotspots": sorted(
            hotspots.values(),
            key=lambda item: (item["count"], item["avg_mfe_pips"]),
            reverse=True,
        )[:12],
        "telemetry_gaps": extracted["telemetry_gaps"],
        "reference_modes": [
            {"reference_mode": mode, "count": count}
            for mode, count in reference_modes.most_common()
        ],
        "results": results,
    }


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}"


def render_rejected_setup_audit(report: dict[str, Any], *, horizon_bars: int, threshold_pips: float | None) -> str:
    summary = report["summary"]
    lines = [
        f"Counterfactual rejected-setup audit ({horizon_bars} M15 bars)",
        (
            "Coverage: "
            f"{summary['audited_candidates']}/{summary['total_reject_like_events']} auditable "
            f"({summary['exact_reference_candidates']} exact, "
            f"{summary['approximate_reference_candidates']} approximate)"
        ),
        f"Telemetry gaps: {summary['telemetry_gap_events']}",
    ]

    if threshold_pips is not None:
        lines.append(
            "Excursion threshold: "
            f"{summary['threshold_hit_candidates']}/{summary['threshold_evaluable_candidates']} "
            f"hit >= {threshold_pips:.1f} pips"
        )

    outcome_bits = [
        f"{row['outcome_label']}={row['count']}"
        for row in report["outcome_summary"]
    ]
    if outcome_bits:
        lines.append("Outcomes: " + ", ".join(outcome_bits))

    if report["reason_hotspots"]:
        lines.append("")
        lines.append("Top counterfactual hotspots:")
        for row in report["reason_hotspots"][:8]:
            lines.append(
                (
                    f"- {row['stage']} {row['reason_code']} {row['symbol']} "
                    f"count={row['count']} avg_mfe={row['avg_mfe_pips']:.2f} "
                    f"avg_mae={row['avg_mae_pips']:.2f} "
                    f"target_first={_fmt_pct(row['target_first_rate'])} "
                    f"threshold_hit={_fmt_pct(row['threshold_hit_rate'])} "
                    f"approx_ref={_fmt_pct(row['approximate_reference_share'])}"
                )
            )

    if report["telemetry_gaps"]:
        lines.append("")
        lines.append("Telemetry blind spots:")
        for row in report["telemetry_gaps"][:8]:
            lines.append(
                f"- {row['stage']} {row['reason_code']} {row['symbol']} count={row['count']}"
            )

    return "\n".join(lines)


def _load_frames_from_csv_dir(csv_dir: Path, symbols: list[str]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        for candidate_name in CSV_CANDIDATE_NAMES:
            candidate_path = csv_dir / candidate_name.format(
                symbol=symbol,
                symbol_lower=symbol.lower(),
            )
            if not candidate_path.exists():
                continue
            df = load_ohlc_csv(candidate_path)
            frames[symbol] = df[["open", "high", "low", "close"]].copy()
            break
    return frames


def _load_frames_from_mt5(symbols: list[str], *, days: int, horizon_bars: int) -> dict[str, pd.DataFrame]:
    load_runtime_env()
    creds = load_mt5_credentials_from_env()
    bridge = MT5Connection(creds.login, creds.password, creds.server)
    if not bridge.connect():
        message = bridge.last_error.message if bridge.last_error else "MT5 connection failed"
        raise RuntimeError(message)
    num_candles = max(int(days) * 24 * 4 + int(horizon_bars) + 64, 256)
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        df = bridge.fetch_ohlc_data(symbol, TIMEFRAME_M15, num_candles)
        if df.empty:
            continue
        frames[symbol] = df[["open", "high", "low", "close"]].copy()
    return frames


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit rejected or blocked setups against forward M15 price action.",
    )
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--policy-mode", default=None)
    parser.add_argument("--execution-mode", default="mt5")
    parser.add_argument("--evidence-stream", default=None)
    parser.add_argument("--account-scope", default=None)
    parser.add_argument("--horizon-bars", type=int, default=16)
    parser.add_argument("--threshold-pips", type=float, default=10.0)
    parser.add_argument("--csv-dir", type=Path, default=None)
    args = parser.parse_args()

    filters = ScopeFilters(
        days=args.days,
        evidence_stream=args.evidence_stream,
        policy_mode=args.policy_mode,
        execution_mode=args.execution_mode,
        account_scope=args.account_scope,
    )

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    extracted = extract_counterfactual_candidates(conn, filters=filters)
    symbols = sorted({candidate.symbol for candidate in extracted["candidates"]})

    if args.csv_dir is not None:
        bars_by_symbol = _load_frames_from_csv_dir(args.csv_dir, symbols)
    else:
        try:
            bars_by_symbol = _load_frames_from_mt5(
                symbols,
                days=args.days,
                horizon_bars=args.horizon_bars,
            )
        except (CredentialsError, RuntimeError) as exc:
            raise SystemExit(f"Unable to load M15 bars for audit: {exc}") from exc

    report = build_rejected_setup_audit(
        conn,
        filters=filters,
        bars_by_symbol=bars_by_symbol,
        horizon_bars=args.horizon_bars,
        threshold_pips=args.threshold_pips,
    )
    conn.close()
    print(
        render_rejected_setup_audit(
            report,
            horizon_bars=args.horizon_bars,
            threshold_pips=args.threshold_pips,
        )
    )


if __name__ == "__main__":
    main()
