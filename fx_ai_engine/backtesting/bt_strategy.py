from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import backtrader as bt
import pandas as pd

from core.account_status import AccountStatus
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15


@dataclass
class BacktestContext:
    symbol: str
    fetch_ohlc: Callable[[str, int, int], pd.DataFrame]
    get_spread: Callable[[str], Optional[float]]


class AgentBacktestStrategy(bt.Strategy):
    params = dict(symbol="EURUSD", base_spread_pips=1.2, lookback=400)

    def __init__(self) -> None:
        self.account_status = AccountStatus()
        self._last_dt = None

        self.context = BacktestContext(
            symbol=self.p.symbol,
            fetch_ohlc=self._fetch_ohlc,
            get_spread=self._get_spread,
        )

        self.regime_agent = RegimeAgent(self.p.symbol, self.context.fetch_ohlc)
        self.technical_agent = TechnicalAgent(
            self.p.symbol, self.context.fetch_ohlc, self.context.get_spread
        )
        self.adversarial_agent = AdversarialAgent(
            self.p.symbol, self.context.fetch_ohlc, self.context.get_spread
        )
        self.portfolio_manager = PortfolioManager()

        # signals kept for backward compatibility with existing test assertions
        self.signals: list[dict] = []
        # results populated when a trade closes
        self.results: list[dict] = []

        self._current_risk_amount: float = 0.0
        self._current_entry_info: dict = {}

    def _get_spread(self, symbol: str) -> Optional[float]:
        pip_value = 0.0001 if "JPY" not in symbol else 0.01
        return self.p.base_spread_pips * pip_value

    def _fetch_ohlc(self, symbol: str, timeframe: int, num_candles: int) -> pd.DataFrame:
        if symbol != self.p.symbol:
            return pd.DataFrame()

        dt = self.data.datetime.datetime(0)
        if self._last_dt is None or dt != self._last_dt:
            self._last_dt = dt

        lines = self.data
        available = min(len(lines.close.array), num_candles)
        if available <= 0:
            return pd.DataFrame()

        idx = pd.to_datetime(lines.datetime.array[-available:], unit="s", utc=True)
        df = pd.DataFrame(
            {
                "open": lines.open.array[-available:],
                "high": lines.high.array[-available:],
                "low": lines.low.array[-available:],
                "close": lines.close.array[-available:],
                "volume": lines.volume.array[-available:],
            },
            index=idx,
        )

        if timeframe == TIMEFRAME_H1:
            df = df.resample("1h").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            )
            df = df.dropna()

        return df

    def next(self) -> None:
        if self.position.size != 0:
            return  # already in a trade — wait for bracket to resolve

        regime = self.regime_agent.evaluate(TIMEFRAME_H1)
        technical = self.technical_agent.evaluate(regime, TIMEFRAME_M15, TIMEFRAME_H1)

        if technical is None:
            return

        adversarial = self.adversarial_agent.evaluate(technical, self.account_status, TIMEFRAME_M15)
        portfolio = self.portfolio_manager.evaluate(technical, adversarial, self.account_status)

        if not portfolio.approved:
            return

        pip_value = 0.0001 if "JPY" not in self.p.symbol else 0.01
        stop_distance = technical.stop_pips * pip_value
        tp_distance = technical.take_profit_pips * pip_value
        if stop_distance <= 0 or tp_distance <= 0:
            return

        cash = self.broker.getvalue()
        risk_amount = cash * portfolio.final_risk_percent
        size = risk_amount / stop_distance
        if size <= 0:
            return

        entry_price = self.data.close[0]
        timestamp = self.data.datetime.datetime(0).isoformat()

        if technical.direction == "BUY":
            stop_price = entry_price - stop_distance
            limit_price = entry_price + tp_distance
            self.buy_bracket(size=size, stopprice=stop_price, limitprice=limit_price)
        else:
            stop_price = entry_price + stop_distance
            limit_price = entry_price - tp_distance
            self.sell_bracket(size=size, stopprice=stop_price, limitprice=limit_price)

        self._current_risk_amount = risk_amount
        self._current_entry_info = {
            "timestamp": timestamp,
            "direction": technical.direction,
            "regime": regime.regime,
        }

        self.signals.append(
            {
                "timestamp": timestamp,
                "regime": regime.regime,
                "technical": technical.reason_code,
                "portfolio": portfolio.reason_code,
            }
        )

    def notify_order(self, order: bt.Order) -> None:
        if order.status in [order.Cancelled, order.Margin, order.Rejected]:
            self._current_risk_amount = 0.0

    def notify_trade(self, trade: bt.Trade) -> None:
        if not trade.isclosed:
            return

        risk = self._current_risk_amount
        r_multiple = trade.pnl / risk if risk > 0 else 0.0

        self.results.append(
            {
                "timestamp": self._current_entry_info.get("timestamp", ""),
                "direction": self._current_entry_info.get("direction", ""),
                "regime": self._current_entry_info.get("regime", ""),
                "pnl": round(trade.pnl, 4),
                "r_multiple": round(r_multiple, 3),
            }
        )
        self._current_risk_amount = 0.0
        self._current_entry_info = {}
