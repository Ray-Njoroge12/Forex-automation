from __future__ import annotations

import argparse
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "database" / "trading_state.db"
LATER_STAGES = ("ADVERSARIAL", "PORTFOLIO", "HARD_RISK", "ML_RANKER", "ROUTER")
DEPLOYABILITY_REASON_CODES = {
    "STRATEGIC_RISK_INELIGIBLE",
    "REJECTED_LOT_PREROUTE",
    "REJECTED_LOT",
}
DETAIL_TOKEN_RE = re.compile(r"([A-Za-z0-9_]+)=([^\s]+)")


@dataclass(frozen=True)
class ScopeFilters:
    days: int
    evidence_stream: str | None = None
    policy_mode: str | None = None
    execution_mode: str | None = "mt5"
    account_scope: str | None = None


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


def _safe_avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _fmt_float(value: float | None, digits: int = 2) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{value:.1%}"


def _parse_detail_tokens(details: str) -> dict[str, str]:
    return {key: value.rstrip(",") for key, value in DETAIL_TOKEN_RE.findall(details or "")}


def _safe_parse_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _parse_bool_token(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _is_deployability_block(*, stage: str, reason_code: str) -> bool:
    return reason_code in DEPLOYABILITY_REASON_CODES or stage in {"STRATEGIC_RISK", "PRE_ROUTE_FEASIBILITY"}


def _adx_band(adx_value: float | None) -> str:
    if adx_value is None:
        return "unknown"
    if adx_value < 12.0:
        return "lt_12"
    if adx_value < 16.0:
        return "12_to_16"
    if adx_value < 20.0:
        return "16_to_20"
    if adx_value < 25.0:
        return "20_to_25"
    return "gte_25"


def _classify_reject_driver(*, stage: str, reason_code: str, details: str) -> str | None:
    tokens = _parse_detail_tokens(details)
    if stage == "TECHNICAL" and reason_code == "TECH_PULLBACK_OR_RSI_INVALID":
        pulled_back = _parse_bool_token(tokens.get("pulled_back"))
        rsi_ok = _parse_bool_token(tokens.get("rsi_ok"))
        if pulled_back is False and rsi_ok is True:
            return "pullback_only"
        if pulled_back is True and rsi_ok is False:
            return "rsi_only"
        if pulled_back is False and rsi_ok is False:
            return "pullback_and_rsi"
        if pulled_back is True and rsi_ok is True:
            return "unexpected_pass_like"
        return "unclassified"
    if stage == "REGIME" and reason_code == "REGIME_NO_TRADE":
        direction = str(tokens.get("direction_candidate") or "UNKNOWN").upper()
        adx_value = _safe_parse_float(tokens.get("adx"))
        return f"direction={direction} adx_band={_adx_band(adx_value)}"
    return None


def _load_trade_rows(conn: sqlite3.Connection, filters: ScopeFilters) -> list[sqlite3.Row]:
    if not _table_exists(conn, "trades"):
        return []
    where_sql, params = _scope_where(conn, "trades", filters=filters, since_column="open_time")
    return conn.execute(
        f"""
        SELECT trade_id, symbol, status, reason_code, profit_loss, r_multiple,
               market_regime, spread_entry, risk_reward, rsi_slope,
               is_london_session, is_newyork_session, open_time, close_time
          FROM trades
          {where_sql}
         ORDER BY id DESC
        """,
        params,
    ).fetchall()


def _load_funnel_rows(conn: sqlite3.Connection, filters: ScopeFilters) -> list[sqlite3.Row]:
    if not _table_exists(conn, "decision_funnel_events"):
        return []
    where_sql, params = _scope_where(conn, "decision_funnel_events", filters=filters, since_column="decision_time")
    return conn.execute(
        f"""
        SELECT decision_time, symbol, stage, outcome, reason_code, trade_id, details
          FROM decision_funnel_events
          {where_sql}
         ORDER BY id DESC
        """,
        params,
    ).fetchall()


def _load_risk_rows(conn: sqlite3.Connection, filters: ScopeFilters) -> list[sqlite3.Row]:
    if not _table_exists(conn, "risk_events"):
        return []
    where_sql, params = _scope_where(conn, "risk_events", filters=filters, since_column="timestamp")
    return conn.execute(
        f"""
        SELECT rule_name, severity, reason, trade_id, timestamp
          FROM risk_events
          {where_sql}
         ORDER BY id DESC
        """,
        params,
    ).fetchall()


def build_tendency_report(
    conn: sqlite3.Connection,
    *,
    filters: ScopeFilters,
) -> dict[str, Any]:
    trades = _load_trade_rows(conn, filters)
    funnel = _load_funnel_rows(conn, filters)
    risks = _load_risk_rows(conn, filters)

    closed_trades = [row for row in trades if str(row["status"] or "").startswith("CLOSED")]
    open_trades = [row for row in trades if str(row["status"] or "") in {"EXECUTED_OPEN", "EXECUTION_UNCERTAIN", "PENDING"}]
    rejected_trades = [row for row in trades if str(row["status"] or "") == "REJECTED"]
    wins = [row for row in closed_trades if float(row["profit_loss"] or 0.0) > 0]
    losses = [row for row in closed_trades if float(row["profit_loss"] or 0.0) < 0]

    technical_pass_trade_ids = {
        str(row["trade_id"])
        for row in funnel
        if row["stage"] == "TECHNICAL" and row["outcome"] == "PASS" and row["trade_id"]
    }
    routed_trade_ids = {
        str(row["trade_id"])
        for row in funnel
        if row["stage"] == "ROUTER" and row["outcome"] == "ROUTED" and row["trade_id"]
    }
    technical_passes = len(technical_pass_trade_ids)
    routed_signals = len(routed_trade_ids)
    routed_after_technical = len(technical_pass_trade_ids & routed_trade_ids)
    decision_cycles = len({str(row["decision_time"]) for row in funnel if row["stage"] == "SESSION"})

    summary = {
        "decision_cycles": decision_cycles,
        "closed_trades": len(closed_trades),
        "open_trades": len(open_trades),
        "rejected_trades": len(rejected_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed_trades)) if closed_trades else None,
        "avg_r": _safe_avg([float(row["r_multiple"] or 0.0) for row in closed_trades]),
        "total_profit": sum(float(row["profit_loss"] or 0.0) for row in closed_trades),
        "technical_passes": technical_passes,
        "routed_signals": routed_signals,
        "route_rate_from_technical_pass": (
            routed_after_technical / technical_passes if technical_passes else None
        ),
        "deployability_blocks": sum(
            1 for row in rejected_trades if str(row["reason_code"] or "") in DEPLOYABILITY_REASON_CODES
        ),
        "strategic_risk_rejects": sum(
            1 for row in rejected_trades if str(row["reason_code"] or "") == "STRATEGIC_RISK_INELIGIBLE"
        ),
        "preroute_lot_rejects": sum(
            1 for row in rejected_trades if str(row["reason_code"] or "") == "REJECTED_LOT_PREROUTE"
        ),
        "late_lot_rejects": sum(1 for row in rejected_trades if row["reason_code"] == "REJECTED_LOT"),
    }

    stage_outcomes = Counter((str(row["stage"]), str(row["outcome"])) for row in funnel)

    rejection_hotspots = Counter()
    deployability_hotspots = Counter()
    reject_drivers = Counter()
    for row in funnel:
        stage = str(row["stage"] or "")
        outcome = str(row["outcome"] or "")
        reason_code = str(row["reason_code"] or "")
        symbol = str(row["symbol"] or "")
        if outcome in {"REJECT", "SKIP"}:
            target = (
                deployability_hotspots
                if _is_deployability_block(stage=stage, reason_code=reason_code)
                else rejection_hotspots
            )
            target[(stage, reason_code, symbol)] += 1
            driver = _classify_reject_driver(stage=stage, reason_code=reason_code, details=str(row["details"] or ""))
            if driver:
                reject_drivers[(stage, reason_code, symbol, driver)] += 1
    for row in rejected_trades:
        reason_code = str(row["reason_code"] or "")
        symbol = str(row["symbol"] or "")
        target = (
            deployability_hotspots
            if reason_code in DEPLOYABILITY_REASON_CODES
            else rejection_hotspots
        )
        target[("BROKER_RESULT", reason_code, symbol)] += 1
    rejection_rows = [
        {
            "stage": stage,
            "reason_code": reason_code,
            "symbol": symbol,
            "count": count,
        }
        for (stage, reason_code, symbol), count in rejection_hotspots.most_common(12)
    ]
    deployability_rows = [
        {
            "stage": stage,
            "reason_code": reason_code,
            "symbol": symbol,
            "count": count,
        }
        for (stage, reason_code, symbol), count in deployability_hotspots.most_common(12)
    ]
    reject_driver_rows = [
        {
            "stage": stage,
            "reason_code": reason_code,
            "symbol": symbol,
            "driver": driver,
            "count": count,
        }
        for (stage, reason_code, symbol, driver), count in reject_drivers.most_common(12)
    ]

    symbol_pressure_map: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "symbol": "",
            "session_pass": 0,
            "regime_pass": 0,
            "regime_reject": 0,
            "technical_pass": 0,
            "technical_reject": 0,
            "technical_skip": 0,
            "later_reject": 0,
            "ml_bypass": 0,
            "routed": 0,
        }
    )
    for row in funnel:
        symbol = str(row["symbol"] or "")
        if not symbol:
            continue
        slot = symbol_pressure_map[symbol]
        slot["symbol"] = symbol
        stage = str(row["stage"] or "")
        outcome = str(row["outcome"] or "")
        if stage == "SESSION" and outcome == "PASS":
            slot["session_pass"] += 1
        elif stage == "REGIME" and outcome == "PASS":
            slot["regime_pass"] += 1
        elif stage == "REGIME" and outcome == "REJECT":
            slot["regime_reject"] += 1
        elif stage == "TECHNICAL" and outcome == "PASS":
            slot["technical_pass"] += 1
        elif stage == "TECHNICAL" and outcome == "REJECT":
            slot["technical_reject"] += 1
        elif stage == "TECHNICAL" and outcome == "SKIP":
            slot["technical_skip"] += 1
        elif stage in LATER_STAGES and outcome == "REJECT":
            slot["later_reject"] += 1
        elif stage == "ML_RANKER" and outcome == "BYPASS":
            slot["ml_bypass"] += 1
        elif stage == "ROUTER" and outcome == "ROUTED":
            slot["routed"] += 1
    symbol_pressure = sorted(symbol_pressure_map.values(), key=lambda item: item["symbol"])

    deployability_map: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "symbol": "",
            "strategic_risk_rejects": 0,
            "preroute_lot_rejects": 0,
            "broker_lot_rejects": 0,
            "latest_minimum_risk_usd": None,
            "latest_max_stop_pips_at_fixed_risk": None,
            "_latest_details_ts": "",
        }
    )
    for row in rejected_trades:
        reason_code = str(row["reason_code"] or "")
        symbol = str(row["symbol"] or "")
        if not symbol or reason_code not in DEPLOYABILITY_REASON_CODES:
            continue
        slot = deployability_map[symbol]
        slot["symbol"] = symbol
        if reason_code == "STRATEGIC_RISK_INELIGIBLE":
            slot["strategic_risk_rejects"] += 1
        elif reason_code == "REJECTED_LOT_PREROUTE":
            slot["preroute_lot_rejects"] += 1
        elif reason_code == "REJECTED_LOT":
            slot["broker_lot_rejects"] += 1
    for row in funnel:
        stage = str(row["stage"] or "")
        outcome = str(row["outcome"] or "")
        symbol = str(row["symbol"] or "")
        if not symbol or outcome != "REJECT" or stage != "STRATEGIC_RISK":
            continue
        slot = deployability_map[symbol]
        slot["symbol"] = symbol
        detail_tokens = _parse_detail_tokens(str(row["details"] or ""))
        minimum_risk = _safe_parse_float(detail_tokens.get("minimum_risk_usd"))
        max_stop = _safe_parse_float(detail_tokens.get("max_stop_pips_at_fixed_risk"))
        decision_time = str(row["decision_time"] or "")
        if decision_time >= slot["_latest_details_ts"] and (minimum_risk is not None or max_stop is not None):
            slot["_latest_details_ts"] = decision_time
            slot["latest_minimum_risk_usd"] = minimum_risk
            slot["latest_max_stop_pips_at_fixed_risk"] = max_stop
    deployability_summary = []
    for symbol, slot in sorted(deployability_map.items()):
        slot.pop("_latest_details_ts", None)
        deployability_summary.append(slot)

    symbol_perf_map: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in closed_trades:
        symbol_perf_map[str(row["symbol"] or "")].append(row)
    symbol_performance = []
    for symbol, rows in sorted(symbol_perf_map.items()):
        pnl_values = [float(row["profit_loss"] or 0.0) for row in rows]
        r_values = [float(row["r_multiple"] or 0.0) for row in rows]
        spreads = [float(row["spread_entry"]) for row in rows if row["spread_entry"] is not None]
        rr_values = [float(row["risk_reward"]) for row in rows if row["risk_reward"] is not None]
        symbol_performance.append(
            {
                "symbol": symbol,
                "closed_trades": len(rows),
                "win_rate": sum(1 for row in rows if float(row["profit_loss"] or 0.0) > 0) / len(rows),
                "avg_r": _safe_avg(r_values),
                "total_profit": sum(pnl_values),
                "avg_spread_entry": _safe_avg(spreads),
                "avg_risk_reward": _safe_avg(rr_values),
            }
        )

    def _feature_slice(rows: list[sqlite3.Row]) -> dict[str, Any]:
        spreads = [float(row["spread_entry"]) for row in rows if row["spread_entry"] is not None]
        rr_values = [float(row["risk_reward"]) for row in rows if row["risk_reward"] is not None]
        slopes = [float(row["rsi_slope"]) for row in rows if row["rsi_slope"] is not None]
        london_share = (
            sum(int(row["is_london_session"] or 0) for row in rows) / len(rows) if rows else None
        )
        ny_share = (
            sum(int(row["is_newyork_session"] or 0) for row in rows) / len(rows) if rows else None
        )
        return {
            "count": len(rows),
            "avg_spread_entry": _safe_avg(spreads),
            "avg_risk_reward": _safe_avg(rr_values),
            "avg_rsi_slope": _safe_avg(slopes),
            "london_share": london_share,
            "newyork_share": ny_share,
        }

    feature_tendencies = {
        "wins": _feature_slice(wins),
        "losses": _feature_slice(losses),
    }

    risk_summary = [
        {
            "rule_name": rule_name,
            "severity": severity,
            "count": count,
        }
        for (rule_name, severity), count in Counter(
            (str(row["rule_name"]), str(row["severity"])) for row in risks
        ).most_common(12)
    ]

    return {
        "filters": filters,
        "summary": summary,
        "stage_outcomes": [
            {"stage": stage, "outcome": outcome, "count": count}
            for (stage, outcome), count in sorted(stage_outcomes.items())
        ],
        "rejection_hotspots": rejection_rows,
        "deployability_hotspots": deployability_rows,
        "reject_drivers": reject_driver_rows,
        "symbol_pressure": symbol_pressure,
        "deployability_summary": deployability_summary,
        "symbol_performance": symbol_performance,
        "feature_tendencies": feature_tendencies,
        "risk_summary": risk_summary,
        "blind_spot_note": (
            "Current telemetry identifies rejection hotspots and realized trade tendencies, "
            "but it does not yet label rejected setups with their later market outcome. "
            "That means false negatives can be prioritized for review, not proven exhaustively."
        ),
    }


def _print_table(title: str, headers: list[str], rows: list[list[str]]) -> None:
    print(f"\n{title}")
    if not rows:
        print("  <no data>")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))
    print("  " + "  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)))
    print("  " + "  ".join("-" * widths[idx] for idx in range(len(headers))))
    for row in rows:
        print("  " + "  ".join(value.ljust(widths[idx]) for idx, value in enumerate(row)))


def print_tendency_report(report: dict[str, Any]) -> None:
    filters: ScopeFilters = report["filters"]
    summary = report["summary"]
    print("=" * 88)
    print("FX AI Engine - Tendency Report")
    print("=" * 88)
    print(
        "Scope: "
        f"days={filters.days} "
        f"policy_mode={filters.policy_mode or 'ALL'} "
        f"execution_mode={filters.execution_mode or 'ALL'} "
        f"evidence_stream={filters.evidence_stream or 'ALL'} "
        f"account_scope={filters.account_scope or 'ALL'}"
    )
    print(
        "Summary: "
        f"cycles={summary['decision_cycles']} "
        f"closed={summary['closed_trades']} wins={summary['wins']} losses={summary['losses']} "
        f"win_rate={_fmt_pct(summary['win_rate'])} avg_r={_fmt_float(summary['avg_r'])} "
        f"total_profit={_fmt_float(summary['total_profit'])} "
        f"tech_passes={summary['technical_passes']} routed={summary['routed_signals']} "
        f"route_rate={_fmt_pct(summary['route_rate_from_technical_pass'])} "
        f"deployability_blocks={summary['deployability_blocks']} "
        f"late_lot_rejects={summary['late_lot_rejects']}"
    )

    _print_table(
        "Stage Outcomes",
        ["stage", "outcome", "count"],
        [
            [row["stage"], row["outcome"], str(row["count"])]
            for row in report["stage_outcomes"]
        ],
    )
    _print_table(
        "Rejection Hotspots",
        ["stage", "reason_code", "symbol", "count"],
        [
            [row["stage"], row["reason_code"], row["symbol"], str(row["count"])]
            for row in report["rejection_hotspots"]
        ],
    )
    _print_table(
        "Deployability Blocks",
        ["stage", "reason_code", "symbol", "count"],
        [
            [row["stage"], row["reason_code"], row["symbol"], str(row["count"])]
            for row in report["deployability_hotspots"]
        ],
    )
    _print_table(
        "Reject Drivers",
        ["stage", "reason_code", "symbol", "driver", "count"],
        [
            [row["stage"], row["reason_code"], row["symbol"], row["driver"], str(row["count"])]
            for row in report["reject_drivers"]
        ],
    )
    _print_table(
        "Symbol Pressure",
        [
            "symbol",
            "session_pass",
            "regime_pass",
            "regime_reject",
            "technical_pass",
            "technical_reject",
            "technical_skip",
            "later_reject",
            "ml_bypass",
            "routed",
        ],
        [
            [
                row["symbol"],
                str(row["session_pass"]),
                str(row["regime_pass"]),
                str(row["regime_reject"]),
                str(row["technical_pass"]),
                str(row["technical_reject"]),
                str(row["technical_skip"]),
                str(row["later_reject"]),
                str(row["ml_bypass"]),
                str(row["routed"]),
            ]
            for row in report["symbol_pressure"]
        ],
    )
    _print_table(
        "Deployability Summary",
        [
            "symbol",
            "strategic_risk",
            "preroute_lot",
            "broker_lot",
            "latest_min_risk",
            "max_stop_at_fixed_risk",
        ],
        [
            [
                row["symbol"],
                str(row["strategic_risk_rejects"]),
                str(row["preroute_lot_rejects"]),
                str(row["broker_lot_rejects"]),
                _fmt_float(row["latest_minimum_risk_usd"], 4),
                _fmt_float(row["latest_max_stop_pips_at_fixed_risk"]),
            ]
            for row in report["deployability_summary"]
        ],
    )
    _print_table(
        "Closed Trade Performance",
        ["symbol", "closed", "win_rate", "avg_r", "total_profit", "avg_spread", "avg_rr"],
        [
            [
                row["symbol"],
                str(row["closed_trades"]),
                _fmt_pct(row["win_rate"]),
                _fmt_float(row["avg_r"]),
                _fmt_float(row["total_profit"]),
                _fmt_float(row["avg_spread_entry"]),
                _fmt_float(row["avg_risk_reward"]),
            ]
            for row in report["symbol_performance"]
        ],
    )
    feature = report["feature_tendencies"]
    _print_table(
        "Feature Tendencies",
        ["slice", "count", "avg_spread", "avg_rr", "avg_rsi_slope", "london_share", "newyork_share"],
        [
            [
                label,
                str(feature[label]["count"]),
                _fmt_float(feature[label]["avg_spread_entry"]),
                _fmt_float(feature[label]["avg_risk_reward"]),
                _fmt_float(feature[label]["avg_rsi_slope"], 4),
                _fmt_pct(feature[label]["london_share"]),
                _fmt_pct(feature[label]["newyork_share"]),
            ]
            for label in ("wins", "losses")
        ],
    )
    _print_table(
        "Risk Event Hotspots",
        ["rule_name", "severity", "count"],
        [
            [row["rule_name"], row["severity"], str(row["count"])]
            for row in report["risk_summary"]
        ],
    )
    print("\nBlind spot:")
    print(f"  {report['blind_spot_note']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze live trading tendencies from SQLite telemetry")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days")
    parser.add_argument("--evidence-stream", default=None)
    parser.add_argument("--policy-mode", default=None)
    parser.add_argument("--execution-mode", default="mt5")
    parser.add_argument("--account-scope", default=None)
    parser.add_argument("--db-path", default=str(DB_PATH))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = build_tendency_report(
            conn,
            filters=ScopeFilters(
                days=args.days,
                evidence_stream=args.evidence_stream,
                policy_mode=args.policy_mode,
                execution_mode=args.execution_mode,
                account_scope=args.account_scope,
            ),
        )
    finally:
        conn.close()
    print_tendency_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
