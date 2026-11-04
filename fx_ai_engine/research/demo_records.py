"""Offline, read-only loaders for demo-account records (wave 3: R7, R8, R9).

Every function here is a pure reader. The SQLite state DB is opened with the
URI flag ``mode=ro`` so a write raises; nothing is ever written to the DB or to
an MT5 terminal bridge folder. Bridge files (deals.json, exit_*.json,
execution_*.json, bars_*_M15.json) are read from copies, never from the live
terminal folder.

Times: deals.json ``time_msc`` is UTC epoch milliseconds (core/file_bridge_connection.py
and core/mt5_bridge.py treat it so). Exit files carry ``close_time`` in broker
SERVER time, so exit times are taken from the matching deal when one exists.

Usage: imported by research.fill_reconciliation, research.drift_monitor and
research.trade_postmortem; there is no CLI.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from backtesting.unified_replay import SYMBOLS, load_bridge_bar_exports
from core.filters.calendar_filter import load_calendar
from research.common import PROJECT_ROOT, parse_ts

EA_MAGIC = 20261103
DEFAULT_STREAM = "runtime_mt5_core_srs"
DEFAULT_THRESHOLDS = Path(__file__).with_name("live_vs_replay_thresholds.json")
DEFAULT_CALENDAR = PROJECT_ROOT / "data" / "economic_calendar.json"
THRESHOLD_SECTIONS = ("version", "r7_fill_reconciliation", "r8_drift_monitor", "r9_postmortem")
BRIDGE_PARTS = ("MQL5", "Files", "bridge")


# ----------------------------------------------------------------------
# SQLite (read-only)
# ----------------------------------------------------------------------
def open_ro(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def snapshot_db(src: str | Path, dst: str | Path) -> Path:
    """Consistent copy of ``src`` (opened read-only) into ``dst`` via the SQLite backup API."""
    src_path, dst_path = Path(src).resolve(), Path(dst).resolve()
    if src_path == dst_path:
        raise ValueError(f"snapshot destination is the source DB: {dst_path}")
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    source = open_ro(src_path)
    try:
        target = sqlite3.connect(dst_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dst_path


def guard_output_dir(path: str | Path) -> Path:
    """Refuse any output directory inside an MT5 terminal bridge folder (MQL5/Files/bridge)."""
    resolved = Path(path).expanduser().resolve()
    parts = resolved.parts
    n = len(BRIDGE_PARTS)
    for i in range(len(parts) - n + 1):
        if tuple(parts[i:i + n]) == BRIDGE_PARTS:
            raise ValueError(f"refusing to write into an MT5 bridge folder: {resolved}")
    return resolved


def load_thresholds(path: str | Path | None = None) -> tuple[dict, str]:
    """Versioned thresholds config and the first 16 hex chars of its sha256."""
    p = Path(path) if path is not None else DEFAULT_THRESHOLDS
    raw = p.read_bytes()
    config = json.loads(raw.decode("utf-8"))
    for key in THRESHOLD_SECTIONS:
        if key not in config:
            raise ValueError(f"thresholds config {p} is missing key {key!r}")
    return config, hashlib.sha256(raw).hexdigest()[:16]


def parse_details(text: str | None) -> dict[str, str]:
    """``k=v k=v`` funnel details to a dict; tokens without '=' are ignored."""
    out: dict[str, str] = {}
    for token in (text or "").split():
        if "=" in token:
            key, value = token.split("=", 1)
            out[key] = value
    return out


def load_trades(conn: sqlite3.Connection, evidence_stream: str = DEFAULT_STREAM) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE evidence_stream = ? ORDER BY id", (evidence_stream,)
    ).fetchall()
    return [dict(r) for r in rows]


def load_funnel(conn: sqlite3.Connection, evidence_stream: str = DEFAULT_STREAM, *,
                trade_id: str | None = None, symbol: str | None = None, stage: str | None = None,
                start: str | None = None, end: str | None = None) -> list[dict]:
    """Decision funnel rows ordered by (decision_time, id); ``start``/``end`` are inclusive ISO strings."""
    sql = "SELECT * FROM decision_funnel_events WHERE evidence_stream = ?"
    params: list[Any] = [evidence_stream]
    for column, op, value in (("trade_id", "=", trade_id), ("symbol", "=", symbol), ("stage", "=", stage),
                              ("decision_time", ">=", start), ("decision_time", "<=", end)):
        if value is not None:
            sql += f" AND {column} {op} ?"
            params.append(value)
    sql += " ORDER BY decision_time, id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def load_execution_feedback(conn: sqlite3.Connection) -> dict[str, dict]:
    """Latest EXECUTION lifecycle event per trade_id, with ``payload`` parsed from payload_json."""
    rows = conn.execute(
        "SELECT * FROM trade_lifecycle_events WHERE event_type = 'EXECUTION' ORDER BY created_at, id"
    ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload_json") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        out[item["trade_id"]] = item
    return out


def load_open_issues(conn: sqlite3.Connection) -> dict[str, set[str]]:
    conflicts = {r[0] for r in conn.execute(
        "SELECT trade_id FROM execution_feedback_conflicts WHERE resolved_at IS NULL")}
    uncertain = {r[0] for r in conn.execute(
        "SELECT trade_id FROM trade_reservations WHERE state = 'EXECUTION_UNCERTAIN'")}
    return {"conflicts": conflicts, "uncertain": uncertain}


# ----------------------------------------------------------------------
# Bridge file copies
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Deal:
    ticket: int
    order: int
    position_id: int
    time: pd.Timestamp
    type: int
    entry: int
    symbol: str
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    magic: int
    comment: str


def _deal(row: Mapping[str, Any]) -> Deal:
    return Deal(
        ticket=int(row["ticket"]), order=int(row.get("order", 0)), position_id=int(row.get("position_id", 0)),
        time=pd.Timestamp(int(row["time_msc"]), unit="ms", tz="UTC"),
        type=int(row.get("type", -1)), entry=int(row.get("entry", -1)), symbol=str(row.get("symbol", "")),
        volume=float(row.get("volume", 0.0)), price=float(row.get("price", 0.0)),
        profit=float(row.get("profit", 0.0)), commission=float(row.get("commission", 0.0)),
        swap=float(row.get("swap", 0.0)), fee=float(row.get("fee", 0.0)),
        magic=int(row.get("magic", 0)), comment=str(row.get("comment", "")),
    )


def load_deals(path: str | Path) -> tuple[list[Deal], dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"unsupported deals.json schema_version: {payload.get('schema_version') if isinstance(payload, dict) else None!r}")
    deals = sorted((_deal(r) for r in payload.get("deals") or []), key=lambda d: (d.time, d.ticket))
    meta = {"generated_at": payload.get("generated_at"),
            "server_utc_offset_seconds": payload.get("server_utc_offset_seconds")}
    return deals, meta


def load_exits(directory: str | Path) -> dict[int, dict]:
    """exit_*.json keyed by position_ticket (files without one, e.g. exit_result.json, are skipped)."""
    d = Path(directory)
    if not d.is_dir():
        return {}
    out: dict[int, dict] = {}
    for path in sorted(d.glob("exit_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("position_ticket"):
            continue
        out[int(data["position_ticket"])] = data
    return out


def load_feedback_files(directory: str | Path) -> dict[str, dict]:
    """execution_*.json (recursive) keyed by trade_id, with sibling payload.json and source_file.

    For duplicate trade_ids keep the first file in sorted path order with a non-zero
    ticket, else the first file.
    """
    d = Path(directory)
    if not d.is_dir():
        return {}
    grouped: dict[str, list[dict]] = {}
    for path in sorted(d.rglob("execution_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("trade_id"):
            continue
        payload_path = path.with_name("payload.json")
        if payload_path.exists():
            data["payload"] = json.loads(payload_path.read_text(encoding="utf-8"))
        data["source_file"] = str(path)
        grouped.setdefault(str(data["trade_id"]), []).append(data)
    return {tid: next((x for x in items if int(x.get("ticket") or 0) != 0), items[0])
            for tid, items in grouped.items()}


def load_m15(bars_dir: str | Path, symbols: Iterable[str] | None = None) -> dict[str, pd.DataFrame]:
    d = Path(bars_dir)
    wanted = list(symbols) if symbols is not None else list(SYMBOLS)
    present = [s for s in wanted if (d / f"bars_{s}_M15.json").exists()]
    frames = load_bridge_bar_exports(d, present)
    return {s: frames[s]["M15"] for s in present if "M15" in frames.get(s, {})}


def load_calendar_events(path: str | Path = DEFAULT_CALENDAR) -> list[dict]:
    events = []
    for event in load_calendar(path):
        try:
            ts = parse_ts(str(event["datetime_utc"]))
        except (KeyError, ValueError):
            continue
        events.append({**event, "time": ts})
    return sorted(events, key=lambda e: e["time"])


def calendar_coverage(events: Sequence[Mapping[str, Any]]) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    times = [e["time"] for e in events if e.get("time") is not None]
    return (min(times), max(times)) if times else (None, None)


# ----------------------------------------------------------------------
# Closed strategy trades
# ----------------------------------------------------------------------
def _ts(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    return parse_ts(str(value))


def closed_trades(conn: sqlite3.Connection, evidence_stream: str = DEFAULT_STREAM,
                  exits: Mapping[int, Mapping[str, Any]] | None = None,
                  deals: Sequence[Deal] | None = None) -> list[dict]:
    """Closed strategy trades from the trades table, with a best-evidence ``exit_time``.

    * status CLOSED_* -> source 'db';
    * status EXECUTED_OPEN with a final exit file for its position_ticket -> source
      'bridge_exit_copy' (status, r_multiple, profit_loss taken from the exit file);
    * exit_time: the closing deal (entry == 1, same position_id), else trades.close_time,
      else the exit file close_time (server time) with ``exit_time_unverified``.
    Deals without a trades row (e.g. EA demo-matrix tests) are never returned.
    """
    exits = exits or {}
    exit_deals: dict[int, Deal] = {}
    for deal in deals or ():
        if deal.entry == 1 and deal.position_id:
            exit_deals.setdefault(deal.position_id, deal)
    out: list[dict] = []
    for trade in load_trades(conn, evidence_stream):
        status = str(trade.get("status") or "")
        position = int(trade.get("position_ticket") or 0)
        exit_file = exits.get(position) if position else None
        if status.startswith("CLOSED_"):
            item = {**trade, "source": "db"}
        elif status == "EXECUTED_OPEN" and exit_file and exit_file.get("is_final_exit") is True:
            item = {**trade, "source": "bridge_exit_copy", "status": exit_file.get("status", status),
                    "r_multiple": float(exit_file.get("r_multiple", 0.0)),
                    "profit_loss": float(exit_file.get("profit_loss", 0.0))}
        else:
            continue
        deal = exit_deals.get(position) if position else None
        file_close = _ts(exit_file.get("close_time")) if exit_file else None
        item["exit_time_unverified"] = False
        if deal is not None:
            item["exit_time"] = deal.time
        elif _ts(trade.get("close_time")) is not None:
            item["exit_time"] = _ts(trade.get("close_time"))
        else:
            item["exit_time"] = file_close
            item["exit_time_unverified"] = True
        if deal is not None and file_close is not None:
            item["exit_file_close_time_offset_s"] = int((file_close - deal.time.floor("s")).total_seconds())
        out.append(item)
    return out
