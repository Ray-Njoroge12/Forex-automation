"""File-backed MT5 connection for hosts without the MetaTrader5 Python package.

The FX_Execution EA writes read-only market observations into the bridge
folder (``market/`` and ``feedback/``). This class reads them and exposes the
same interface the engine uses on ``MT5Connection`` (ADR 0001, decision D4).

Every read fails closed: stale, missing, malformed or inconsistent files make
the method return an empty frame, ``None`` or an error dictionary, and set
``last_error`` with a reason code. Nothing here can place or modify orders.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from core.market_quote_snapshot import QuoteSnapshotError, load_demo_quotes
from core.mt5_bridge import (
    MT5Connection,
    Preserve10ApprovalFacts,
    SymbolExecutionContract,
    _closed_history_summary_from_deals,
    account_unit_scale,
    usd_from_account_units,
)

logger = logging.getLogger("fx_ai_engine.file_bridge")

# MT5 timeframe constants used by the engine, mapped to EA export names.
TIMEFRAME_NAMES: dict[int, str] = {15: "M15", 16385: "H1", 16388: "H4", 16408: "D1"}
TIMEFRAME_SECONDS: dict[str, int] = {"M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}

STATUS_MAX_AGE_SECONDS = 30.0
ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS = 30.0
DEALS_MAX_AGE_SECONDS = 120.0
SYMBOL_SPECS_MAX_AGE_SECONDS = 300.0
QUOTE_SNAPSHOT_MAX_AGE_SECONDS = 15.0
QUOTE_TICK_MAX_AGE_SECONDS = 60.0
FUTURE_TOLERANCE_SECONDS = 5.0

# MT5 enum values needed to summarise deals without the MetaTrader5 module.
DEAL_CONSTANTS = SimpleNamespace(
    DEAL_ENTRY_IN=0,
    DEAL_ENTRY_OUT=1,
    DEAL_ENTRY_INOUT=2,
    DEAL_ENTRY_OUT_BY=3,
    ORDER_TYPE_BUY=0,
    ORDER_TYPE_SELL=1,
)

BAR_COLUMNS = ["time_utc", "open", "high", "low", "close", "tick_volume", "spread_points"]


class FileBridgeDataError(ValueError):
    """An EA export cannot safely be used.

    ``reason_code`` (class default or per-instance) lets a precise code survive
    the conversion to ``BridgeError``; ``None`` means the caller's default applies.
    """

    reason_code: str | None = None

    def __init__(self, message: object = "", *, reason_code: str | None = None):
        super().__init__(message)
        if reason_code:
            self.reason_code = reason_code


class FileBridgeSymbolError(FileBridgeDataError):
    """The EA could not resolve one or more broker symbols (``symbols_ok`` is false)."""

    reason_code = "FILE_BRIDGE_SYMBOLS_NOT_OK"


def _code_for(exc: BaseException, default: str) -> str:
    """Reason code carried by *exc*, else *default*."""
    return getattr(exc, "reason_code", None) or default


def _field_invalid(message: str) -> FileBridgeDataError:
    return FileBridgeDataError(message, reason_code="FILE_BRIDGE_STATUS_FIELD_INVALID")


def _parse_utc(value: object) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise FileBridgeDataError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise FileBridgeDataError(f"timestamp without timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


@dataclass
class _CachedFile:
    mtime_ns: int
    size: int
    payload: Any


class FileBridgeConnection(MT5Connection):
    """Reads EA exports from ``<bridge>/market`` and ``<bridge>/feedback``."""

    def __init__(self, bridge_path: Path, *, clock=None):
        super().__init__(login=0, password="", server="")
        self.bridge_path = Path(bridge_path)
        self.market_dir = self.bridge_path / "market"
        self.feedback_dir = self.bridge_path / "feedback"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[Path, _CachedFile] = {}
        self._last_currency: str | None = None

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return self._clock()

    def _read_json(self, path: Path) -> Any:
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise FileBridgeDataError(f"missing file: {path.name}") from exc
        cached = self._cache.get(path)
        if cached and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size:
            return cached.payload
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FileBridgeDataError(f"unreadable or malformed file: {path.name}: {exc}") from exc
        self._cache[path] = _CachedFile(stat.st_mtime_ns, stat.st_size, payload)
        return payload

    def _require_fresh(self, stamp: object, max_age: float, label: str) -> float:
        generated = _parse_utc(stamp)
        age = (self._now() - generated).total_seconds()
        if age < -FUTURE_TOLERANCE_SECONDS:
            raise FileBridgeDataError(f"{label} timestamp is in the future (age={age:.1f}s)")
        if age > max_age:
            raise FileBridgeDataError(f"{label} is stale (age={age:.1f}s max={max_age:.0f}s)")
        return age

    @staticmethod
    def _validate_profile_fields(status: dict[str, Any]) -> None:
        """Shape-check the optional EA profile fields; absent fields (old EA) are fine."""
        if "symbol_suffix" in status and not isinstance(status["symbol_suffix"], str):
            raise _field_invalid("status symbol_suffix must be a string")
        if "symbols_ok" in status and type(status["symbols_ok"]) is not bool:
            raise _field_invalid("status symbols_ok must be a boolean")
        if "symbol_errors" in status:
            errors = status["symbol_errors"]
            if not isinstance(errors, list) or not all(isinstance(item, str) for item in errors):
                raise _field_invalid("status symbol_errors must be a list of strings")
            if errors and status.get("symbols_ok") is True:
                raise _field_invalid("status symbol_errors is non-empty while symbols_ok is true")
        if "account_currency" in status and (
            not isinstance(status["account_currency"], str) or not status["account_currency"].strip()
        ):
            raise _field_invalid("status account_currency must be a non-empty string")
        if "max_spread_pips_input" in status:
            value = status["max_spread_pips_input"]
            if not _finite_number(value) or value < 0:
                raise _field_invalid("status max_spread_pips_input must be a finite number >= 0")
        if "live_guard" in status and not isinstance(status["live_guard"], dict):
            raise _field_invalid("status live_guard must be an object")
        if "account_margin_mode" in status:
            mode = status["account_margin_mode"]
            if isinstance(mode, bool) or not isinstance(mode, (str, int)):
                raise _field_invalid("status account_margin_mode must be a string or an integer")

    def _read_status(self, *, require_symbols_ok: bool) -> dict[str, Any]:
        status = self._read_json(self.market_dir / "status.json")
        if not isinstance(status, dict) or status.get("schema_version") != 1:
            raise FileBridgeDataError("unsupported market status schema")
        self._require_fresh(status.get("generated_at"), STATUS_MAX_AGE_SECONDS, "market status")
        if status.get("server_offset_valid") is not True:
            raise FileBridgeDataError("EA could not determine the server time offset")
        if status.get("terminal_connected") is not True:
            raise FileBridgeDataError("MT5 terminal reports no server connection")
        self._validate_profile_fields(status)
        # Gate on symbols_ok=false, and also on a non-empty symbol_errors when symbols_ok is absent.
        if require_symbols_ok and (status.get("symbols_ok") is False or status.get("symbol_errors")):
            listed = ", ".join(status.get("symbol_errors") or []) or "unspecified"
            raise FileBridgeSymbolError(
                f"EA could not resolve broker symbols (suffix={status.get('symbol_suffix', '')!r}): {listed}"
            )
        return status

    def _status(self) -> dict[str, Any]:
        return self._read_status(require_symbols_ok=True)

    def get_ea_profile(self) -> dict[str, Any]:
        """Validated EA profile fields (``None`` when absent) from a fresh status file.

        Does not apply the ``symbols_ok`` gate so a checker can still report it.
        Raises ``FileBridgeDataError`` when the status file is unusable.
        """
        status = self._read_status(require_symbols_ok=False)
        keys = (
            "ea_version", "symbol_suffix", "symbols_ok", "symbol_errors",
            "account_margin_mode", "account_currency", "max_spread_pips_input", "live_guard",
        )
        profile: dict[str, Any] = {key: status.get(key) for key in keys}
        if isinstance(profile["symbol_errors"], list):
            profile["symbol_errors"] = list(profile["symbol_errors"])
        if isinstance(profile["live_guard"], dict):
            profile["live_guard"] = dict(profile["live_guard"])
        return profile

    def server_utc_offset_seconds(self) -> int | None:
        """Broker server time minus UTC from a fresh, valid status file, else ``None``."""
        try:
            offset = self._status().get("server_utc_offset_seconds")
        except ValueError as exc:  # FileBridgeDataError or an unparsable timestamp
            self._fail(_code_for(exc, "FILE_BRIDGE_STATUS_UNAVAILABLE"), exc)
            return None
        return offset if type(offset) is int else None

    def _account_snapshot_payload(self) -> dict[str, Any]:
        snapshot = self._read_json(self.feedback_dir / "account_snapshot.json")
        if not isinstance(snapshot, dict):
            raise FileBridgeDataError("account snapshot is not an object")
        self._require_fresh(snapshot.get("timestamp"), ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS, "account snapshot")
        status = self._status()
        login, server = status.get("login"), status.get("server")
        if (type(login) is not int or login <= 0 or not isinstance(server, str) or not server.strip()
                or type(snapshot.get("login")) is not int or snapshot.get("login") != login
                or snapshot.get("server") != server):
            raise FileBridgeDataError("account identity differs between status and account snapshot")
        if self.connected and (self.login != login or self.server != server):
            raise FileBridgeDataError("terminal account changed since connection")
        status_currency, snapshot_currency = status.get("account_currency"), snapshot.get("currency")
        if (isinstance(status_currency, str) and status_currency.strip()
                and isinstance(snapshot_currency, str) and snapshot_currency.strip()
                and status_currency.strip().upper() != snapshot_currency.strip().upper()):
            raise FileBridgeDataError(
                f"account currency differs between status ({status_currency}) and account snapshot ({snapshot_currency})",
                reason_code="FILE_BRIDGE_ACCOUNT_CURRENCY_MISMATCH",
            )
        if snapshot.get("account_authenticated") is not True or snapshot.get("terminal_connected") is not True:
            raise FileBridgeDataError("account snapshot does not confirm authentication and connectivity")
        if type(snapshot.get("account_is_demo")) is not bool or snapshot.get("account_is_demo") != status.get("account_is_demo"):
            raise FileBridgeDataError("account type differs between status and account snapshot")
        for key in ("balance", "equity", "margin_free", "floating_pnl"):
            if not _finite_number(snapshot.get(key)):
                raise FileBridgeDataError(f"account snapshot {key} is not a finite number")
        currency = snapshot.get("currency")
        if isinstance(currency, str) and currency.strip():
            self._last_currency = currency
        return snapshot

    def _account_currency(self) -> str | None:
        """Account currency from validated evidence, else the last validated value, else ``None``.

        Evidence is a fresh, schema-valid status.json (``account_currency``) or a
        fresh account snapshot (``currency``), read without the ``symbols_ok``
        gate so history stays available. ``None`` means the currency is unknown;
        it is never defaulted to USD. When both files name a currency and they
        differ, the answer is ``None``. Records no error: callers do.
        """
        found: list[str] = []
        try:
            value = self._read_status(require_symbols_ok=False).get("account_currency")
            if isinstance(value, str) and value.strip():
                found.append(value)
        except ValueError:  # FileBridgeDataError or an unparsable timestamp
            pass
        try:
            snapshot = self._read_json(self.feedback_dir / "account_snapshot.json")
            if isinstance(snapshot, dict):
                self._require_fresh(snapshot.get("timestamp"), ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS, "account snapshot")
                value = snapshot.get("currency")
                if isinstance(value, str) and value.strip():
                    found.append(value)
        except ValueError:
            pass
        if len({item.strip().upper() for item in found}) > 1:
            return None
        if found:
            self._last_currency = found[0]
            return found[0]
        return self._last_currency

    def _unit_scale(self) -> float | None:
        """Account-currency to USD multiplier, or ``None`` (with a bridge error) when the currency is unknown."""
        currency = self._account_currency()
        if currency is None:
            self._fail(
                "FILE_BRIDGE_ACCOUNT_CURRENCY_UNKNOWN",
                "account currency cannot be established from a fresh status or account snapshot",
            )
            return None
        return account_unit_scale(currency)

    def _fail(self, code: str, exc: Exception | str) -> None:
        self._make_error(code, str(exc))

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    def connect(self, retry_count: int = 3) -> bool:
        if not self.bridge_path.is_dir():
            self._fail("FILE_BRIDGE_PATH_MISSING", f"bridge folder not found: {self.bridge_path}")
            return False
        last_exc: Exception | None = None
        for attempt in range(max(1, retry_count)):
            try:
                status = self._status()
                login = status.get("login")
                if type(login) is not int or login <= 0:
                    raise FileBridgeDataError("EA reports no authenticated account")
                if not isinstance(status.get("server"), str) or not status["server"].strip():
                    raise FileBridgeDataError("EA reports no account server")
                self.login = login
                self.server = str(status.get("server") or "")
                currency = status.get("account_currency")
                if isinstance(currency, str) and currency.strip():
                    self._last_currency = currency
                self.connected = True
                self.last_error = None
                logger.info(
                    "File bridge connected path=%s login=%s server=%s ea_version=%s symbol_suffix=%r",
                    self.bridge_path, self.login, self.server, status.get("ea_version"),
                    status.get("symbol_suffix", ""),
                )
                return True
            except FileBridgeDataError as exc:
                last_exc = exc
                if attempt + 1 < retry_count:
                    time.sleep(2)
        self.connected = False
        self._fail(
            _code_for(last_exc, "FILE_BRIDGE_NOT_READY") if last_exc else "FILE_BRIDGE_NOT_READY",
            last_exc or "unknown",
        )
        return False

    def _ensure_connected(self) -> bool:
        if self.connected:
            return True
        return self.connect(retry_count=1)

    def shutdown(self) -> None:
        self.connected = False

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def _account_info(self) -> Any:
        try:
            snapshot = self._account_snapshot_payload()
        except FileBridgeDataError as exc:
            self._fail(_code_for(exc, "FILE_BRIDGE_ACCOUNT_UNAVAILABLE"), exc)
            return None
        return SimpleNamespace(
            balance=float(snapshot["balance"]),
            equity=float(snapshot["equity"]),
            margin_free=float(snapshot["margin_free"]),
            currency=str(snapshot.get("currency") or "USD"),
            trade_mode=0 if snapshot.get("account_is_demo") is True else 2,
        )

    def is_demo_account(self) -> bool | None:
        try:
            snapshot = self._account_snapshot_payload()
            status = self._status()
        except FileBridgeDataError as exc:
            self._fail(_code_for(exc, "FILE_BRIDGE_ACCOUNT_UNAVAILABLE"), exc)
            return None
        flags = (snapshot.get("account_is_demo"), status.get("account_is_demo"))
        if all(flag is True for flag in flags):
            return True
        if any(flag is False for flag in flags):
            return False
        return None

    def get_account_snapshot(self) -> dict[str, Any] | None:
        try:
            snapshot = self._account_snapshot_payload()
        except FileBridgeDataError as exc:
            return {"error": self._make_error(_code_for(exc, "FILE_BRIDGE_ACCOUNT_UNAVAILABLE"), str(exc)).__dict__}
        result = dict(snapshot)
        scale = account_unit_scale(snapshot.get("currency") or self._account_currency())
        if scale != 1.0:
            for key in ("balance", "equity", "margin_free", "floating_pnl"):
                result[key] = usd_from_account_units(snapshot[key], scale)
            result["money_unit"] = "USD"
            result["unit_scale"] = scale
        return result

    def get_open_position_tickets(self) -> list[int] | None:
        try:
            snapshot = self._account_snapshot_payload()
        except FileBridgeDataError as exc:
            self._fail(_code_for(exc, "FILE_BRIDGE_ACCOUNT_UNAVAILABLE"), exc)
            return None
        managed = snapshot.get("managed_position_tickets")
        unmanaged = snapshot.get("unmanaged_position_tickets")
        if not isinstance(managed, list) or not isinstance(unmanaged, list):
            self._fail("FILE_BRIDGE_POSITIONS_UNAVAILABLE", "snapshot lacks position ticket lists")
            return None
        entries = managed + unmanaged
        count = snapshot.get("open_positions_count")
        if (type(count) is not int or count < 0
                or any(type(ticket) is not int or ticket <= 0 for ticket in entries)):
            self._fail("FILE_BRIDGE_POSITIONS_UNAVAILABLE", "invalid position count or ticket")
            return None
        tickets = set(entries)
        if len(tickets) != len(entries) or len(tickets) != count:
            self._fail(
                "FILE_BRIDGE_POSITIONS_INCONSISTENT",
                f"tickets={len(tickets)} open_positions_count={snapshot.get('open_positions_count')}",
            )
            return None
        return sorted(tickets)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def _deals(self) -> list[SimpleNamespace] | None:
        try:
            payload = self._read_json(self.market_dir / "deals.json")
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise FileBridgeDataError("unsupported deal history schema")
            self._require_fresh(payload.get("generated_at"), DEALS_MAX_AGE_SECONDS, "deal history")
            rows = payload.get("deals")
            if not isinstance(rows, list):
                raise FileBridgeDataError("deal history must be a list")
            deals = []
            for row in rows:
                if not isinstance(row, dict):
                    raise FileBridgeDataError("deal row is not an object")
                time_msc = int(row.get("time_msc") or 0)
                deals.append(SimpleNamespace(
                    ticket=int(row.get("ticket") or 0),
                    order=int(row.get("order") or 0),
                    position_id=int(row.get("position_id") or 0),
                    time_msc=time_msc,
                    time=time_msc // 1000,
                    type=int(row.get("type", -1)),
                    entry=int(row.get("entry", -1)),
                    symbol=str(row.get("symbol") or ""),
                    volume=float(row.get("volume") or 0.0),
                    price=float(row.get("price") or 0.0),
                    profit=float(row.get("profit") or 0.0),
                    commission=float(row.get("commission") or 0.0),
                    swap=float(row.get("swap") or 0.0),
                    fee=float(row.get("fee") or 0.0),
                    magic=int(row.get("magic") or 0),
                    comment=str(row.get("comment") or ""),
                ))
            return deals
        except (FileBridgeDataError, TypeError, ValueError) as exc:
            self._fail(_code_for(exc, "FILE_BRIDGE_HISTORY_UNAVAILABLE"), exc)
            return None

    def get_position_history_summary(self, position_ticket: int, *, lookback_days: int = 30) -> dict[str, Any] | None:
        if position_ticket <= 0:
            return None
        deals = self._deals()
        if deals is None:
            return None
        matched = [d for d in deals if d.position_id == int(position_ticket)]
        unit_scale = self._unit_scale()
        if unit_scale is None:
            return None
        return _closed_history_summary_from_deals(
            matched,
            position_ticket_hint=int(position_ticket),
            constants=DEAL_CONSTANTS,
            unit_scale=unit_scale,
        )

    def get_trade_history_summary(self, trade_id: str, *, lookback_days: int = 30) -> dict[str, Any] | None:
        trade_id = str(trade_id or "").strip()
        if not trade_id:
            return None
        deals = self._deals()
        if deals is None:
            return None
        seed = [d for d in deals if d.comment.strip() == trade_id]
        if not seed:
            return None
        positions = {d.position_id for d in seed if d.position_id > 0}
        matched = [d for d in deals if d.position_id in positions] if positions else seed
        unit_scale = self._unit_scale()
        if unit_scale is None:
            return None
        return _closed_history_summary_from_deals(
            matched, trade_id_hint=trade_id, constants=DEAL_CONSTANTS, unit_scale=unit_scale
        )

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def _load_bars(self, symbol: str, tf_name: str) -> pd.DataFrame:
        status = self._status()
        latest = status.get("latest_closed_bars")
        if not isinstance(latest, dict) or not isinstance(latest.get(symbol), dict):
            raise FileBridgeDataError(f"EA has no closed {tf_name} bar for {symbol}")
        expected = latest[symbol].get(tf_name)
        if type(expected) is not int or expected <= 0:
            raise FileBridgeDataError(f"EA has no closed {tf_name} bar for {symbol}")
        payload = self._read_json(self.market_dir / f"bars_{symbol}_{tf_name}.json")
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise FileBridgeDataError("unsupported bar export schema")
        if payload.get("symbol") != symbol or payload.get("timeframe") != tf_name:
            raise FileBridgeDataError("bar export symbol/timeframe mismatch")
        if payload.get("columns") != BAR_COLUMNS:
            raise FileBridgeDataError("unexpected bar export columns")
        rows = payload.get("bars")
        if not isinstance(rows, list) or not rows:
            raise FileBridgeDataError("bar export is empty")
        previous = None
        for row in rows:
            if not isinstance(row, list) or len(row) != len(BAR_COLUMNS) or not all(_finite_number(v) for v in row):
                raise FileBridgeDataError("malformed bar row")
            t, o, h, low, c = row[0], row[1], row[2], row[3], row[4]
            if type(t) is not int or t <= 0:
                raise FileBridgeDataError("bar time must be a positive integer")
            if min(o, h, low, c) <= 0 or h < max(o, c) or low > min(o, c):
                raise FileBridgeDataError(f"inconsistent OHLC at time={t}")
            if previous is not None and t <= previous:
                raise FileBridgeDataError("bar times are not strictly increasing")
            previous = t
        last_time = int(rows[-1][0])
        step = TIMEFRAME_SECONDS[tf_name]
        # status.json is rewritten every few seconds but a bar file on bar close,
        # so at a bar boundary the bar file can be exactly one bar ahead of the
        # status it is checked against. Accept that one bar; the future-close
        # check below still rejects a bar that has not closed yet.
        if last_time < expected:
            raise FileBridgeDataError(
                f"bar export lags the terminal (last={last_time} expected={expected})"
            )
        if last_time not in (expected, expected + step):
            raise FileBridgeDataError(
                "bar export is more than one bar ahead of the terminal status "
                f"(last={last_time} expected={expected})"
            )
        bar_close = last_time + step
        if bar_close > self._now().timestamp() + FUTURE_TOLERANCE_SECONDS + 60:
            raise FileBridgeDataError("latest closed bar ends in the future; check server offset")
        frame = pd.DataFrame(rows, columns=BAR_COLUMNS)
        frame = frame.rename(columns={"time_utc": "time", "spread_points": "spread"})
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        frame["tick_volume"] = frame["tick_volume"].astype("int64")
        frame["spread"] = frame["spread"].astype("int64")
        frame["real_volume"] = 0
        frame = frame.set_index("time")
        frame.attrs["server_utc_offset_seconds"] = payload.get("server_utc_offset_seconds")
        return frame

    def fetch_ohlc_data(self, symbol: str, timeframe: int, num_candles: int = 300) -> pd.DataFrame:
        tf_name = TIMEFRAME_NAMES.get(int(timeframe))
        if tf_name is None:
            err = self._make_error("FILE_BRIDGE_TIMEFRAME_UNSUPPORTED", f"timeframe={timeframe}")
            df = pd.DataFrame()
            df.attrs["error"] = err.__dict__
            return df
        try:
            frame = self._load_bars(symbol, tf_name)
        except FileBridgeDataError as exc:
            err = self._make_error(
                _code_for(exc, "FILE_BRIDGE_OHLC_UNAVAILABLE"), f"symbol={symbol} tf={tf_name}: {exc}"
            )
            df = pd.DataFrame()
            df.attrs["error"] = err.__dict__
            return df
        result = frame.tail(max(1, int(num_candles))).copy()
        result.attrs = dict(frame.attrs)
        return result

    def get_live_spread(self, symbol: str) -> float | None:
        try:
            self._status()  # symbols_ok gate and fresh-status check
        except ValueError as exc:  # FileBridgeDataError or an unparsable timestamp
            self._fail(_code_for(exc, "FILE_BRIDGE_STATUS_UNAVAILABLE"), exc)
            return None
        try:
            quotes = load_demo_quotes(
                self.feedback_dir / "market_quotes.json",
                now=self._now(),
                max_snapshot_age=QUOTE_SNAPSHOT_MAX_AGE_SECONDS,
                max_quote_age=QUOTE_TICK_MAX_AGE_SECONDS,
            )
        except QuoteSnapshotError as exc:
            self._fail("FILE_BRIDGE_QUOTES_UNAVAILABLE", exc)
            return None
        quote = quotes.get(symbol)
        if quote is None:
            self._fail("FILE_BRIDGE_QUOTE_STALE", f"no fresh quote for {symbol}")
            return None
        return float(quote.spread)

    def get_symbol_execution_contract(self, symbol: str) -> SymbolExecutionContract | None:
        try:
            self._status()  # symbols_ok gate and fresh-status check
            payload = self._read_json(self.market_dir / "symbols.json")
            if not isinstance(payload, dict) or payload.get("schema_version") != 1:
                raise FileBridgeDataError("unsupported symbol spec schema")
            self._require_fresh(payload.get("generated_at"), SYMBOL_SPECS_MAX_AGE_SECONDS, "symbol specs")
            spec = payload.get("symbols", {}).get(symbol)
            if not isinstance(spec, dict):
                raise FileBridgeDataError(f"no symbol spec for {symbol}")
            if "broker_symbol" in spec and (
                not isinstance(spec["broker_symbol"], str) or not spec["broker_symbol"].strip()
            ):
                raise _field_invalid(f"symbol spec broker_symbol for {symbol} must be a non-empty string")
            values = {k: spec.get(k) for k in ("volume_min", "volume_step", "volume_max", "tick_value", "tick_size", "point")}
            if not all(_finite_number(v) and v > 0 for v in values.values()):
                raise FileBridgeDataError(f"invalid symbol spec values for {symbol}")
        except FileBridgeDataError as exc:
            self._fail(_code_for(exc, "FILE_BRIDGE_SYMBOL_SPEC_UNAVAILABLE"), exc)
            return None
        unit_scale = self._unit_scale()
        if unit_scale is None:
            return None
        return SymbolExecutionContract(
            symbol=symbol,
            min_lot=float(values["volume_min"]),
            lot_step=float(values["volume_step"]),
            max_lot=float(values["volume_max"]),
            tick_value=usd_from_account_units(float(values["tick_value"]), unit_scale),
            tick_size=float(values["tick_size"]),
            point=float(values["point"]),
        )

    def get_preserve_10_approval_facts(self, *, now: datetime | None = None, **_: Any) -> Preserve10ApprovalFacts:
        fetched = (now or self._now()).isoformat()
        return self._approval_failure(
            "FILE_BRIDGE_PRESERVE_10_UNSUPPORTED",
            "Preserve-$10 broker approval facts are not exported by the file bridge",
            fetched_at_utc=fetched,
        )
