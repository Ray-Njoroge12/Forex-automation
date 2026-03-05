from __future__ import annotations

import logging
from dataclasses import dataclass
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


@dataclass(frozen=True)
class BridgeError:
    code: str
    message: str
    timestamp_utc: str


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

    def shutdown(self) -> None:
        """Safely closes MT5 connection."""
        if self.connected and mt5 is not None:
            mt5.shutdown()
            logger.info("MT5 connection closed")
        self.connected = False
