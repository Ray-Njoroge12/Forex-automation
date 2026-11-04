"""Offline, read-only loaders for demo-account records (wave 3: R7, R8, R9).

Every function here is a pure reader. The SQLite state DB is opened with the
URI flag ``mode=ro`` so a write raises; nothing is ever written to the DB or to
an MT5 terminal bridge folder. Bridge files (deals.json, exit_*.json,
execution_*.json, bars_*_M15.json) are read from copies, never from the live
terminal folder.

Times: deals.json ``time_msc`` is UTC epoch milliseconds (core/file_bridge_connection.py
and core/mt5_bridge.py treat it so). Exit files carry ``close_time`` in broker
SERVER time, so exit times are taken from the final closing deal when one exists.

R: live trades are compared with the replay on net R (``R_DEFINITION``), computed
from the position's deals, because the EA's ``r_multiple`` leaves out commission
and swap while the replay's ``r_multiple`` is net of commission.

Usage: imported by research.fill_reconciliation, research.drift_monitor and
research.trade_postmortem; there is no CLI.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from backtesting.unified_replay import SYMBOLS, load_bridge_bar_exports, pip_value_usd_per_lot
from core.filters.calendar_filter import load_calendar
from core.mt5_bridge import account_unit_scale, usd_from_account_units
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
STANDARD_CONTRACT_SIZE = 100000.0


@dataclass(frozen=True)
class AccountUnits:
    """Money and volume units of the bridge copies (cent accounts: USC, contract size 1,000)."""
    currency: str = "USD"
    unit_scale: float = 1.0
    contract_sizes: Mapping[str, float] = field(default_factory=dict)
    source: str = "default"

    def money(self, value: Any) -> Any:
        return usd_from_account_units(value, self.unit_scale)

    def volume(self, symbol: str, value: float) -> float:
        """Standard-lot equivalent: volume x contract_size / 100000."""
        factor = float(self.contract_sizes.get(symbol, STANDARD_CONTRACT_SIZE)) / STANDARD_CONTRACT_SIZE
        return value if factor == 1.0 else value * factor

    @property
    def is_default(self) -> bool:
        return self.unit_scale == 1.0 and all(float(v) == STANDARD_CONTRACT_SIZE for v in self.contract_sizes.values())

    def describe(self) -> dict:
        return {"currency": self.currency, "unit_scale": self.unit_scale,
                "contract_sizes": dict(sorted(self.contract_sizes.items())), "source": self.source,
                "money": "converted to USD", "volume": "standard-lot equivalent (volume x contract_size / 100000)"}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def resolve_units(deals_path: str | Path, account_currency: str | None = None,
                  symbols_path: str | Path | None = None) -> AccountUnits:
    """Units of a deals.json copy: currency from the argument, else status.json ``account_currency``
    beside the deals file (or in its parent, the bridge root), else USD; contract sizes from
    ``symbols_path``, else symbols.json beside the deals file, else 100000. Missing or unreadable
    sibling files fall back to the default silently."""
    deals_dir = Path(deals_path).parent
    sources = []
    currency = account_currency
    if currency:
        sources.append("--account-currency")
    else:
        for candidate in (deals_dir / "status.json", deals_dir.parent / "status.json"):
            data = _read_json(candidate)
            if isinstance(data, dict) and data.get("account_currency"):
                currency = str(data["account_currency"])
                sources.append(str(candidate))
                break
    sym_file = Path(symbols_path) if symbols_path else deals_dir / "symbols.json"
    sizes: dict[str, float] = {}
    data = _read_json(sym_file)
    if isinstance(data, dict) and isinstance(data.get("symbols"), dict):
        for name, spec in data["symbols"].items():
            try:
                size = float(spec["contract_size"])
            except (KeyError, TypeError, ValueError):
                continue
            if size > 0:
                sizes[str(name)] = size
        if sizes:
            sources.append(str(sym_file))
    currency = currency or "USD"
    return AccountUnits(currency=currency, unit_scale=account_unit_scale(currency), contract_sizes=sizes,
                        source=", ".join(sources) or "default")


def _convert_pl(data: dict, units: AccountUnits | None) -> dict:
    if units is not None and not units.is_default and isinstance(data.get("profit_loss"), (int, float)):
        data["profit_loss"] = units.money(data["profit_loss"])
    return data


@dataclass(frozen=True)
class Deal:
    """One MT5 deal. With units, money is USD and ``volume`` is a standard-lot equivalent."""
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


def load_deals(path: str | Path, units: AccountUnits | None = None) -> tuple[list[Deal], dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"unsupported deals.json schema_version: {payload.get('schema_version') if isinstance(payload, dict) else None!r}")
    deals = [_deal(r) for r in payload.get("deals") or []]
    if units is not None and not units.is_default:
        deals = [replace(d, profit=units.money(d.profit), commission=units.money(d.commission),
                         swap=units.money(d.swap), fee=units.money(d.fee), volume=units.volume(d.symbol, d.volume))
                 for d in deals]
    deals = sorted(deals, key=lambda d: (d.time, d.ticket))
    meta = {"generated_at": payload.get("generated_at"),
            "server_utc_offset_seconds": payload.get("server_utc_offset_seconds")}
    return deals, meta


def load_exits(directory: str | Path, units: AccountUnits | None = None) -> dict[int, dict]:
    """exit_*.json keyed by position_ticket (files without one, e.g. exit_result.json, are skipped)."""
    d = Path(directory)
    if not d.is_dir():
        return {}
    out: dict[int, dict] = {}
    for path in sorted(d.glob("exit_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("position_ticket"):
            continue
        out[int(data["position_ticket"])] = _convert_pl(data, units)
    return out


def load_feedback_files(directory: str | Path, units: AccountUnits | None = None) -> dict[str, dict]:
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
        _convert_pl(data, units)
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


def _opt_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


R_DEFINITION = ("net R = sum over the position's deals of (profit + commission + swap + fee) / risk USD, with "
                "risk USD = lots x stop pips (trades.stop_loss) x USD pip value at the entry price; the same "
                "definition as unified_replay's r_multiple (net_usd / risk_usd). The EA's r_multiple is price-based "
                "and leaves out commission and swap.")


def net_r_fields(trade: Mapping[str, Any], entry_deals: Sequence[Deal], exit_deals: Sequence[Deal],
                 lot_factor: float = 1.0) -> dict[str, Any]:
    """Net R of one position from its deals, defined as unified_replay defines r_multiple (net_usd / risk_usd).

    Returns risk_usd, net_usd, net_r and, when net_r is None, net_r_missing (the reason).
    """
    symbol = str(trade.get("symbol") or "")
    lots = entry_deals[0].volume if entry_deals else _opt_float(trade.get("lot_size"))
    if lots and not entry_deals:
        lots = lots * lot_factor
    entry_price = entry_deals[0].price if entry_deals else _opt_float(trade.get("entry_price"))
    stop = _opt_float(trade.get("stop_loss"))
    risk = None
    if symbol in SYMBOLS and lots and stop and stop > 0 and entry_price:
        risk = lots * stop * pip_value_usd_per_lot(symbol, entry_price)
    out: dict[str, Any] = {"risk_usd": None if risk is None else round(risk, 4), "net_usd": None, "net_r": None}
    if not exit_deals:
        out["net_r_missing"] = "no closing deal"
        return out
    net = sum(d.profit + d.commission + d.swap + d.fee for d in (*entry_deals, *exit_deals))
    out["net_usd"] = round(net, 4)
    if risk is None or risk <= 0:
        out["net_r_missing"] = "risk USD unknown (lots, stop pips or entry price missing)"
        return out
    out["net_r"] = round(net / risk, 6) + 0.0
    return out


def closed_trades(conn: sqlite3.Connection, evidence_stream: str = DEFAULT_STREAM,
                  exits: Mapping[int, Mapping[str, Any]] | None = None,
                  deals: Sequence[Deal] | None = None, units: AccountUnits | None = None) -> list[dict]:
    """Closed strategy trades from the trades table, with a best-evidence ``exit_time`` and net R.

    * status CLOSED_* -> source 'db';
    * status EXECUTED_OPEN with a final exit file for its position_ticket -> source
      'bridge_exit_copy' (status, r_multiple, profit_loss taken from the exit file; a
      missing value stays None);
    * exit_time: the FINAL closing deal (last deal with entry == 1 and the same
      position_id, so an EA partial close is not taken as the exit), else
      trades.close_time, else the exit file close_time. Both fallbacks come from the
      EA exit payload, which carries server time, so they set ``exit_time_unverified``;
      ``exit_time_source`` names the source;
    * net_r / net_usd / risk_usd: see ``R_DEFINITION`` and ``net_r_fields`` (None without
      deals). ``r_multiple`` stays the EA's gross, price-based value.
    Deals without a trades row (e.g. EA demo-matrix tests) are never returned.
    """
    exits = exits or {}
    entry_deals: dict[int, list[Deal]] = {}
    exit_deals: dict[int, list[Deal]] = {}
    for deal in sorted(deals or (), key=lambda d: (d.time, d.ticket)):
        if deal.position_id and deal.entry in (0, 1):
            (entry_deals if deal.entry == 0 else exit_deals).setdefault(deal.position_id, []).append(deal)
    out: list[dict] = []
    for trade in load_trades(conn, evidence_stream):
        status = str(trade.get("status") or "")
        position = int(trade.get("position_ticket") or 0)
        exit_file = exits.get(position) if position else None
        if status.startswith("CLOSED_"):
            item = {**trade, "source": "db"}
        elif status == "EXECUTED_OPEN" and exit_file and exit_file.get("is_final_exit") is True:
            item = {**trade, "source": "bridge_exit_copy", "status": exit_file.get("status", status),
                    "r_multiple": _opt_float(exit_file.get("r_multiple")),
                    "profit_loss": _opt_float(exit_file.get("profit_loss"))}
        else:
            continue
        closing = exit_deals.get(position, []) if position else []
        final = closing[-1] if closing else None
        file_close = _ts(exit_file.get("close_time")) if exit_file else None
        db_close = _ts(trade.get("close_time"))
        if final is not None:
            item.update(exit_time=final.time, exit_time_source="deal", exit_time_unverified=False)
        elif db_close is not None:
            item.update(exit_time=db_close, exit_time_source="trades.close_time", exit_time_unverified=True)
        else:
            item.update(exit_time=file_close, exit_time_source="exit_file.close_time" if file_close else None,
                        exit_time_unverified=True)
        item["closing_deals"] = len(closing)
        if final is not None and file_close is not None:
            item["exit_file_close_time_offset_s"] = int((file_close - final.time.floor("s")).total_seconds())
        if deals is not None:
            symbol = str(trade.get("symbol") or "")
            lot_factor = units.volume(symbol, 1.0) if units is not None else 1.0
            item.update(net_r_fields(trade, entry_deals.get(position, []) if position else [], closing,
                                     lot_factor=lot_factor))
        else:
            item.update(risk_usd=None, net_usd=None, net_r=None, net_r_missing="no deals given")
        out.append(item)
    return out
