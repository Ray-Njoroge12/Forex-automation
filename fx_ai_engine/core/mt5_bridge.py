from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import os

import pandas as pd

mt5 = None
try:
    if os.getenv("USE_MT5_MOCK") == "1":
        try:
            import MMetaTrader5 as mt5  # type: ignore
        except Exception:
            from core import mt5_mock as mt5  # type: ignore
    else:
        try:
            import mt5_wrapper as mt5  # type: ignore
        except Exception:
            import MetaTrader5 as mt5  # type: ignore
except Exception:  # pragma: no cover - handled by runtime checks
    mt5 = None

logger = logging.getLogger("fx_ai_engine.mt5_bridge")

PRESERVE_10_REQUIRED_SYMBOLS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
)

_ACCOUNT_DENOMINATION_MAP: dict[str, tuple[str, float]] = {
    "USD": ("usd", 1.0),
    "USC": ("usd_cent", 0.01),
    "USCENT": ("usd_cent", 0.01),
    "USDCENT": ("usd_cent", 0.01),
    "CENTUSD": ("usd_cent", 0.01),
}


@dataclass(frozen=True)
class BridgeError:
    code: str
    message: str
    timestamp_utc: str


@dataclass(frozen=True)
class SymbolExecutionContract:
    symbol: str
    min_lot: float
    lot_step: float
    max_lot: float
    tick_value: float
    tick_size: float
    point: float


@dataclass(frozen=True)
class TradeFeasibilityDecision:
    can_assess: bool
    approved: bool
    reason_code: str
    details: str
    estimated_lot: float = 0.0


@dataclass(frozen=True)
class Preserve10AccountFacts:
    currency: str
    denomination: str
    unit_scale: float
    reported_balance: float
    reported_equity: float
    normalized_balance_usd: float
    normalized_equity_usd: float
    leverage: int
    trade_allowed: bool


@dataclass(frozen=True)
class Preserve10SymbolFacts:
    symbol: str
    trade_mode: int
    tradable: bool
    volume_min: float
    volume_step: float
    volume_max: float
    contract_size: float
    tick_value: float
    tick_size: float
    point: float
    digits: int
    stops_level: int
    freeze_level: int
    spread_price: float
    spread_pips: float
    min_lot_margin: float
    quote_time_utc: str
    quote_age_seconds: int


@dataclass(frozen=True)
class Preserve10ApprovalFacts:
    can_assess: bool
    reason_code: str
    details: str
    fetched_at_utc: str
    account: Preserve10AccountFacts | None = None
    symbols: dict[str, Preserve10SymbolFacts] = field(default_factory=dict)


class MT5Connection:
    def __init__(self, login: int, password: str, server: str):
        self.login = login
        self.password = password
        self.server = server
        self.connected = False
        self.last_error: BridgeError | None = None

    def _make_error(self, code: str, message: str) -> BridgeError:
        error = BridgeError(
            code=code,
            message=message,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )
        self.last_error = error
        logger.error("%s: %s", code, message)
        return error

    def _require_mt5(self) -> bool:
        if mt5 is None:
            self._make_error(
                "MT5_MODULE_MISSING",
                "MetaTrader5 package is not available. Install dependencies first.",
            )
            return False
        return True

    def connect(self, retry_count: int = 3) -> bool:
        """Establishes connection to local MT5 terminal with retries."""
        if not self._require_mt5():
            return False

        for i in range(retry_count):
            logger.info("Initializing MT5 connection for server=%s (attempt %d/%d)", self.server, i+1, retry_count)
            ok = mt5.initialize(login=self.login, server=self.server, password=self.password)
            if ok:
                self.connected = True
                self.last_error = None
                logger.info("Connected to MT5 server=%s", self.server)
                return True
            
            err = mt5.last_error()
            logger.warning("MT5 init failed attempt %d: %s", i+1, err)
            import time
            time.sleep(1)

        self._make_error(
            "MT5_INIT_FAILED",
            f"Initialization failed after {retry_count} attempts, last_error={mt5.last_error()}",
        )
        self.connected = False
        return False

    def _ensure_connected(self) -> bool:
        """Checks if connection is alive, attempts reconnect if not."""
        if not self.connected:
            return self.connect()
        
        # Test connection with a lightweight call
        try:
            if mt5.terminal_info() is None:
                logger.warning("MT5 connection lost (terminal_info is None), reconnecting...")
                self.connected = False
                return self.connect()
        except AttributeError:
            # Mocks or alternative environments might not have terminal_info.
            # Safely bypass check if attribute doesn't exist.
            pass
        
        return True

    def get_account_snapshot(self) -> dict[str, Any] | None:
        """Returns live account state or explicit error object."""
        if not self._ensure_connected():
            return {
                "error": self._make_error(
                    "MT5_NOT_CONNECTED",
                    "Cannot fetch account snapshot because MT5 is not connected.",
                ).__dict__
            }

        account_info = mt5.account_info() if mt5 is not None else None
        if account_info is None:
            return {
                "error": self._make_error(
                    "MT5_ACCOUNT_INFO_FAILED",
                    f"Failed account_info, mt5.last_error={mt5.last_error() if mt5 else 'n/a'}",
                ).__dict__
            }

        positions = mt5.positions_get() if mt5 is not None else None
        open_positions = list(positions) if positions else []

        floating_pnl = float(sum(getattr(pos, "profit", 0.0) for pos in open_positions))

        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "balance": float(account_info.balance),
            "equity": float(account_info.equity),
            "margin_free": float(account_info.margin_free),
            "open_positions_count": int(len(open_positions)),
            "open_symbols": list(set(pos.symbol for pos in open_positions)),
            "floating_pnl": floating_pnl,
        }
        logger.info(
            "Account snapshot fetched balance=%.2f equity=%.2f open_positions=%d",
            snapshot["balance"],
            snapshot["equity"],
            snapshot["open_positions_count"],
        )
        return snapshot

    def fetch_ohlc_data(
        self, symbol: str, timeframe: int, num_candles: int = 300
    ) -> pd.DataFrame:
        """Fetches broker-native OHLC data; returns empty frame with attrs['error'] on failure."""
        if not self._ensure_connected():
            err = self._make_error(
                "MT5_NOT_CONNECTED",
                "Cannot fetch OHLC because MT5 is not connected.",
            )
            df = pd.DataFrame()
            df.attrs["error"] = err.__dict__
            return df

        selected = mt5.symbol_select(symbol, True) if mt5 is not None else False
        if not selected:
            err = self._make_error(
                "SYMBOL_SELECT_FAILED",
                f"Failed to select symbol={symbol}, mt5.last_error={mt5.last_error() if mt5 else 'n/a'}",
            )
            df = pd.DataFrame()
            df.attrs["error"] = err.__dict__
            return df

        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, num_candles) if mt5 else None
        if rates is None:
            err = self._make_error(
                "OHLC_FETCH_FAILED",
                f"Failed to fetch rates for symbol={symbol}, mt5.last_error={mt5.last_error() if mt5 else 'n/a'}",
            )
            df = pd.DataFrame()
            df.attrs["error"] = err.__dict__
            return df

        df = pd.DataFrame(rates)
        if df.empty:
            err = self._make_error(
                "OHLC_EMPTY",
                f"OHLC response empty for symbol={symbol}, timeframe={timeframe}",
            )
            df.attrs["error"] = err.__dict__
            return df

        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)

        logger.info(
            "Fetched OHLC symbol=%s timeframe=%s candles=%d",
            symbol,
            timeframe,
            len(df),
        )
        return df

    def get_live_spread(self, symbol: str) -> float | None:
        """Returns spread as ask-bid or None on failure."""
        if not self._ensure_connected():
            self._make_error(
                "MT5_NOT_CONNECTED",
                "Cannot fetch spread because MT5 is not connected.",
            )
            return None

        tick = mt5.symbol_info_tick(symbol) if mt5 is not None else None
        if tick is None:
            self._make_error(
                "SPREAD_FETCH_FAILED",
                f"Failed symbol_info_tick for symbol={symbol}, mt5.last_error={mt5.last_error() if mt5 else 'n/a'}",
            )
            return None

        spread = float(tick.ask - tick.bid)
        logger.info("Spread fetched symbol=%s spread=%f", symbol, spread)
        return spread

    def get_symbol_execution_contract(self, symbol: str) -> SymbolExecutionContract | None:
        """Return broker-side volume/tick contract needed for lot feasibility checks."""
        if not self._ensure_connected():
            self._make_error(
                "MT5_NOT_CONNECTED",
                f"Cannot fetch symbol execution contract for symbol={symbol} because MT5 is not connected.",
            )
            return None

        if not (mt5.symbol_select(symbol, True) if mt5 is not None else False):
            self._make_error(
                "SYMBOL_SELECT_FAILED",
                f"Failed to select symbol={symbol}, mt5.last_error={mt5.last_error() if mt5 else 'n/a'}",
            )
            return None

        info = mt5.symbol_info(symbol) if mt5 is not None else None
        if info is None:
            self._make_error(
                "SYMBOL_INFO_FAILED",
                f"Failed symbol_info for symbol={symbol}, mt5.last_error={mt5.last_error() if mt5 else 'n/a'}",
            )
            return None

        return SymbolExecutionContract(
            symbol=symbol,
            min_lot=float(getattr(info, "volume_min", 0.0) or 0.0),
            lot_step=float(getattr(info, "volume_step", 0.0) or 0.0),
            max_lot=float(getattr(info, "volume_max", 0.0) or 0.0),
            tick_value=float(
                getattr(info, "trade_tick_value", getattr(info, "tick_value", 0.0)) or 0.0
            ),
            tick_size=float(
                getattr(info, "trade_tick_size", getattr(info, "tick_size", 0.0)) or 0.0
            ),
            point=float(getattr(info, "point", 0.0) or 0.0),
        )

    def _approval_failure(
        self,
        code: str,
        details: str,
        *,
        fetched_at_utc: str,
        account: Preserve10AccountFacts | None = None,
        symbols: dict[str, Preserve10SymbolFacts] | None = None,
    ) -> Preserve10ApprovalFacts:
        self._make_error(code, details)
        return Preserve10ApprovalFacts(
            can_assess=False,
            reason_code=code,
            details=details,
            fetched_at_utc=fetched_at_utc,
            account=account,
            symbols=dict(symbols or {}),
        )

    def _normalize_preserve_10_account_facts(
        self,
        account_info: Any,
    ) -> tuple[Preserve10AccountFacts | None, str | None, str | None]:
        raw_currency = str(getattr(account_info, "currency", "") or "").strip()
        currency_key = "".join(ch for ch in raw_currency.upper() if ch.isalnum())
        mapped = _ACCOUNT_DENOMINATION_MAP.get(currency_key)
        if mapped is None:
            return (
                None,
                "APPROVAL_ACCOUNT_DENOMINATION_UNKNOWN",
                f"unsupported account denomination currency={raw_currency!r}",
            )

        trade_allowed_attr = getattr(account_info, "trade_allowed", None)
        leverage = int(getattr(account_info, "leverage", 0) or 0)
        if trade_allowed_attr is None or leverage <= 0:
            return (
                None,
                "APPROVAL_ACCOUNT_FACTS_INVALID",
                (
                    f"account facts invalid currency={raw_currency!r} leverage={leverage} "
                    f"trade_allowed_present={trade_allowed_attr is not None}"
                ),
            )

        balance = float(getattr(account_info, "balance", 0.0) or 0.0)
        equity = float(getattr(account_info, "equity", 0.0) or 0.0)
        denomination, unit_scale = mapped
        return (
            Preserve10AccountFacts(
                currency=raw_currency,
                denomination=denomination,
                unit_scale=unit_scale,
                reported_balance=balance,
                reported_equity=equity,
                normalized_balance_usd=round(balance * unit_scale, 8),
                normalized_equity_usd=round(equity * unit_scale, 8),
                leverage=leverage,
                trade_allowed=bool(trade_allowed_attr),
            ),
            None,
            None,
        )

    def _normalize_preserve_10_symbol_facts(
        self,
        symbol: str,
        *,
        now: datetime,
        max_quote_age_seconds: int,
    ) -> tuple[Preserve10SymbolFacts | None, str | None, str | None]:
        if not (mt5.symbol_select(symbol, True) if mt5 is not None else False):
            return (
                None,
                "APPROVAL_SYMBOL_UNAVAILABLE",
                f"symbol_select failed for symbol={symbol}",
            )

        info = mt5.symbol_info(symbol) if mt5 is not None else None
        if info is None:
            return (
                None,
                "APPROVAL_SYMBOL_INFO_MISSING",
                f"symbol_info unavailable for symbol={symbol}",
            )

        tick = mt5.symbol_info_tick(symbol) if mt5 is not None else None
        if tick is None:
            return (
                None,
                "APPROVAL_SYMBOL_TICK_MISSING",
                f"symbol_info_tick unavailable for symbol={symbol}",
            )

        quote_timestamp = float(getattr(tick, "time_msc", 0) or 0)
        if quote_timestamp > 0:
            quote_dt = datetime.fromtimestamp(quote_timestamp / 1000.0, tz=timezone.utc)
        else:
            quote_seconds = int(getattr(tick, "time", getattr(info, "time", 0)) or 0)
            if quote_seconds <= 0:
                return (
                    None,
                    "APPROVAL_SYMBOL_TICK_MISSING",
                    f"quote timestamp unavailable for symbol={symbol}",
                )
            quote_dt = datetime.fromtimestamp(quote_seconds, tz=timezone.utc)

        quote_age_seconds = int((now - quote_dt).total_seconds())
        if quote_age_seconds < -5:
            return (
                None,
                "APPROVAL_SYMBOL_FACTS_INCONSISTENT",
                f"quote timestamp is in the future for symbol={symbol} age_seconds={quote_age_seconds}",
            )
        if quote_age_seconds > max_quote_age_seconds:
            return (
                None,
                "APPROVAL_SYMBOL_TICK_STALE",
                (
                    f"quote too old for symbol={symbol} age_seconds={quote_age_seconds} "
                    f"max_age_seconds={max_quote_age_seconds}"
                ),
            )

        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        if ask <= 0 or bid <= 0 or ask < bid:
            return (
                None,
                "APPROVAL_SYMBOL_FACTS_INCONSISTENT",
                f"invalid tick prices for symbol={symbol} bid={bid:.8f} ask={ask:.8f}",
            )

        trade_mode = getattr(info, "trade_mode", None)
        volume_min = float(getattr(info, "volume_min", 0.0) or 0.0)
        volume_step = float(getattr(info, "volume_step", 0.0) or 0.0)
        volume_max = float(getattr(info, "volume_max", 0.0) or 0.0)
        contract_size = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
        tick_value = float(
            getattr(info, "trade_tick_value", getattr(info, "tick_value", 0.0)) or 0.0
        )
        tick_size = float(
            getattr(info, "trade_tick_size", getattr(info, "tick_size", 0.0)) or 0.0
        )
        point = float(getattr(info, "point", 0.0) or 0.0)
        digits = int(getattr(info, "digits", -1) if getattr(info, "digits", None) is not None else -1)
        stops_level = int(getattr(info, "trade_stops_level", 0) or 0)
        freeze_level = int(getattr(info, "trade_freeze_level", 0) or 0)

        if trade_mode is None:
            return (
                None,
                "APPROVAL_SYMBOL_FACTS_INVALID",
                f"trade_mode missing for symbol={symbol}",
            )

        if any(value <= 0 for value in (volume_min, volume_step, volume_max, contract_size, tick_value, tick_size, point)) or digits < 0:
            return (
                None,
                "APPROVAL_SYMBOL_FACTS_INVALID",
                (
                    f"missing or non-positive symbol facts symbol={symbol} volume_min={volume_min:.6f} "
                    f"volume_step={volume_step:.6f} volume_max={volume_max:.6f} "
                    f"contract_size={contract_size:.6f} tick_value={tick_value:.8f} "
                    f"tick_size={tick_size:.8f} point={point:.8f} digits={digits}"
                ),
            )

        if volume_max < volume_min or volume_step > volume_max:
            return (
                None,
                "APPROVAL_SYMBOL_FACTS_INCONSISTENT",
                (
                    f"inconsistent lot grid symbol={symbol} volume_min={volume_min:.6f} "
                    f"volume_step={volume_step:.6f} volume_max={volume_max:.6f}"
                ),
            )

        margin_calc = getattr(mt5, "order_calc_margin", None)
        if margin_calc is None:
            return (
                None,
                "APPROVAL_MARGIN_UNAVAILABLE",
                f"order_calc_margin unavailable for symbol={symbol}",
            )

        order_type_buy = getattr(mt5, "ORDER_TYPE_BUY", 0) if mt5 is not None else 0
        min_lot_margin = margin_calc(order_type_buy, symbol, volume_min, ask)
        min_lot_margin_value = float(min_lot_margin or 0.0)
        if min_lot_margin_value <= 0:
            return (
                None,
                "APPROVAL_MARGIN_UNAVAILABLE",
                f"min-lot margin unavailable for symbol={symbol}",
            )

        spread_price = ask - bid
        pip_value = 0.01 if symbol.endswith("JPY") else 0.0001
        disabled_trade_mode = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0) if mt5 is not None else 0
        return (
            Preserve10SymbolFacts(
                symbol=symbol,
                trade_mode=int(trade_mode),
                tradable=int(trade_mode) != int(disabled_trade_mode),
                volume_min=volume_min,
                volume_step=volume_step,
                volume_max=volume_max,
                contract_size=contract_size,
                tick_value=tick_value,
                tick_size=tick_size,
                point=point,
                digits=digits,
                stops_level=stops_level,
                freeze_level=freeze_level,
                spread_price=round(spread_price, 8),
                spread_pips=round(spread_price / pip_value, 8),
                min_lot_margin=round(min_lot_margin_value, 8),
                quote_time_utc=quote_dt.isoformat(),
                quote_age_seconds=quote_age_seconds,
            ),
            None,
            None,
        )

    def get_preserve_10_approval_facts(
        self,
        *,
        symbols: tuple[str, ...] = PRESERVE_10_REQUIRED_SYMBOLS,
        max_quote_age_seconds: int = 120,
        now: datetime | None = None,
    ) -> Preserve10ApprovalFacts:
        """Fetch fail-closed account and symbol facts required for Preserve-$10 approval."""
        now_utc = now or datetime.now(timezone.utc)
        fetched_at_utc = now_utc.isoformat()
        if not self._ensure_connected():
            return self._approval_failure(
                "MT5_NOT_CONNECTED",
                "Cannot fetch Preserve-$10 approval facts because MT5 is not connected.",
                fetched_at_utc=fetched_at_utc,
            )

        account_info = mt5.account_info() if mt5 is not None else None
        if account_info is None:
            return self._approval_failure(
                "APPROVAL_ACCOUNT_INFO_MISSING",
                "account_info unavailable for Preserve-$10 approval facts",
                fetched_at_utc=fetched_at_utc,
            )

        account, account_code, account_details = self._normalize_preserve_10_account_facts(account_info)
        if account is None:
            return self._approval_failure(
                account_code or "APPROVAL_ACCOUNT_FACTS_INVALID",
                account_details or "account facts unavailable",
                fetched_at_utc=fetched_at_utc,
            )

        symbol_facts: dict[str, Preserve10SymbolFacts] = {}
        for symbol in symbols:
            facts, code, details = self._normalize_preserve_10_symbol_facts(
                symbol,
                now=now_utc,
                max_quote_age_seconds=max_quote_age_seconds,
            )
            if facts is None:
                return self._approval_failure(
                    code or "APPROVAL_SYMBOL_FACTS_INVALID",
                    details or f"symbol facts unavailable for symbol={symbol}",
                    fetched_at_utc=fetched_at_utc,
                    account=account,
                    symbols=symbol_facts,
                )
            symbol_facts[symbol] = facts

        self.last_error = None
        return Preserve10ApprovalFacts(
            can_assess=True,
            reason_code="APPROVAL_FACTS_READY",
            details=f"Preserve-$10 approval facts ready for {len(symbol_facts)} symbols",
            fetched_at_utc=fetched_at_utc,
            account=account,
            symbols=symbol_facts,
        )

    def evaluate_trade_feasibility(
        self,
        symbol: str,
        risk_percent: float,
        stop_pips: float,
        *,
        account_balance: float | None = None,
    ) -> TradeFeasibilityDecision:
        """Mirror the EA's lot math to detect pre-route min-lot infeasibility."""
        contract = self.get_symbol_execution_contract(symbol)
        if contract is None:
            return TradeFeasibilityDecision(
                can_assess=False,
                approved=True,
                reason_code="BROKER_CONTRACT_UNAVAILABLE",
                details=(
                    "cannot run pre-route lot check because broker execution contract data is unavailable "
                    f"for symbol={symbol}"
                ),
            )

        if account_balance is None:
            account_info = mt5.account_info() if mt5 is not None else None
            account_balance = float(getattr(account_info, "balance", 0.0) or 0.0)

        if account_balance <= 0:
            return TradeFeasibilityDecision(
                can_assess=False,
                approved=True,
                reason_code="ACCOUNT_BALANCE_UNAVAILABLE",
                details=(
                    "cannot run pre-route lot check because account balance is unavailable or non-positive "
                    f"for symbol={symbol}"
                ),
            )

        if (
            risk_percent <= 0
            or stop_pips <= 0
            or contract.tick_value <= 0
            or contract.tick_size <= 0
            or contract.point <= 0
            or contract.min_lot <= 0
            or contract.lot_step <= 0
        ):
            return TradeFeasibilityDecision(
                can_assess=False,
                approved=True,
                reason_code="BROKER_CONTRACT_INVALID",
                details=(
                    "cannot run pre-route lot check because feasibility inputs are invalid "
                    f"for symbol={symbol} risk_percent={risk_percent:.8f} stop_pips={stop_pips:.2f} "
                    f"min_lot={contract.min_lot:.4f} lot_step={contract.lot_step:.4f} "
                    f"tick_value={contract.tick_value:.8f} tick_size={contract.tick_size:.8f} "
                    f"point={contract.point:.8f}"
                ),
            )

        pip_value = 0.01 if "JPY" in symbol else 0.0001
        risk_amount = account_balance * risk_percent
        value_per_point = contract.tick_value / contract.tick_size * contract.point
        stop_points = stop_pips * pip_value / contract.point
        raw_limit = (risk_amount / (stop_points * value_per_point)) if stop_points > 0 else 0.0

        if raw_limit < contract.min_lot:
            return TradeFeasibilityDecision(
                can_assess=True,
                approved=False,
                reason_code="REJECTED_LOT_PREROUTE",
                details=(
                    "trade blocked before MT5 routing because the preserve-first lot estimate is below the broker minimum "
                    f"for symbol={symbol} balance={account_balance:.2f} risk_percent={risk_percent:.8f} "
                    f"risk_amount={risk_amount:.4f} stop_pips={stop_pips:.2f} raw_limit={raw_limit:.6f} "
                    f"min_lot={contract.min_lot:.4f}"
                ),
            )

        estimated_lot = math.floor(raw_limit / contract.lot_step) * contract.lot_step
        if contract.max_lot > 0:
            estimated_lot = min(estimated_lot, contract.max_lot)
        estimated_lot = round(max(estimated_lot, 0.0), 8)

        return TradeFeasibilityDecision(
            can_assess=True,
            approved=True,
            reason_code="TRADE_FEASIBLE",
            details=(
                "pre-route lot check passed "
                f"for symbol={symbol} balance={account_balance:.2f} risk_percent={risk_percent:.8f} "
                f"raw_limit={raw_limit:.6f} estimated_lot={estimated_lot:.6f} min_lot={contract.min_lot:.4f}"
            ),
            estimated_lot=estimated_lot,
        )

    def shutdown(self) -> None:
        """Safely closes MT5 connection."""
        if self.connected and mt5 is not None:
            mt5.shutdown()
            logger.info("MT5 connection closed")
        self.connected = False
