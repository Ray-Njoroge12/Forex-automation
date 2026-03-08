from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable, Optional

import backtrader as bt
import pandas as pd

from backtesting.simulation_profile import (
    assess_trade_feasibility,
    build_simulation_profile,
    convert_quote_pnl_to_usd,
)
from core.account_status import AccountStatus
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from config_microcapital import get_policy_config
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15


@dataclass
class BacktestContext:
    symbol: str
    fetch_ohlc: Callable[[str, int, int], pd.DataFrame]
    get_spread: Callable[[str], Optional[float]]


class AgentBacktestStrategy(bt.Strategy):
    params = dict(symbol="EURUSD", simulation_profile=None, lookback=400)

    def __init__(self) -> None:
        self.simulation_profile = self.p.simulation_profile or build_simulation_profile()
        self.policy = get_policy_config(mode_id=self.simulation_profile.mode_id)
        self.account_status = AccountStatus()
        self._last_dt = None
        self.simulated_balance_usd = float(self.simulation_profile.starting_cash)
        self.peak_simulated_balance_usd = float(self.simulation_profile.starting_cash)
        self.max_simulated_drawdown_pct = 0.0

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
            self.p.symbol,
            self.context.fetch_ohlc,
            self.context.get_spread,
            policy=self.policy,
        )
        self.portfolio_manager = PortfolioManager(
            fixed_risk_usd=self.simulation_profile.fixed_risk_usd,
            fetch_ohlc=self.context.fetch_ohlc,
            policy=self.policy,
        )

        # signals kept for backward compatibility with existing test assertions
        self.signals: list[dict] = []
        # results populated when a trade closes
        self.results: list[dict] = []
        self.rejected_signals: list[dict] = []
        self.funnel_events: list[dict] = []
        self.funnel_counts: Counter[str] = Counter()

        self._current_risk_amount: float = 0.0
        self._current_entry_info: dict = {}

    def _record_funnel(self, stage: str, outcome: str, reason_code: str) -> None:
        self.funnel_events.append(
            {
                "timestamp": self.data.datetime.datetime(0).isoformat(),
                "symbol": self.p.symbol,
                "stage": stage,
                "outcome": outcome,
                "reason_code": reason_code,
            }
        )
        self.funnel_counts[f"{stage}:{outcome}"] += 1

    def _get_spread(self, symbol: str) -> Optional[float]:
        pip_value = 0.0001 if "JPY" not in symbol else 0.01
        return self.simulation_profile.base_spread_pips * pip_value

    def _current_balance(self) -> float:
        if self.simulation_profile.realistic_constraints:
            return self.simulated_balance_usd
        return float(self.broker.getvalue())

    def _refresh_account_status(self) -> None:
        balance = self._current_balance()
        self.account_status.balance = balance
        self.account_status.equity = balance
        self.account_status.open_positions_count = 1 if self.position.size != 0 else 0
        self.account_status.open_symbols = [self.p.symbol] if self.position.size != 0 else []

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
        self._refresh_account_status()
        if self.position.size != 0:
            return  # already in a trade — wait for bracket to resolve

        regime = self.regime_agent.evaluate(TIMEFRAME_H1)
        if regime.regime in {"TRENDING_BULL", "TRENDING_BEAR"}:
            self._record_funnel("REGIME", "PASS", regime.regime)
        else:
            self._record_funnel("REGIME", "REJECT", regime.regime)
        technical = self.technical_agent.evaluate(regime, TIMEFRAME_M15, TIMEFRAME_H1)

        if technical is None:
            self._record_funnel("TECHNICAL", "REJECT", self.technical_agent.last_reason_code)
            return
        self._record_funnel("TECHNICAL", "PASS", technical.reason_code)

        adversarial = self.adversarial_agent.evaluate(technical, self.account_status, TIMEFRAME_M15)
        if adversarial.approved:
            self._record_funnel("ADVERSARIAL", "PASS", adversarial.reason_code)
        else:
            self._record_funnel("ADVERSARIAL", "REJECT", adversarial.reason_code)
        portfolio = self.portfolio_manager.evaluate(technical, adversarial, self.account_status)

        if not portfolio.approved:
            self._record_funnel("PORTFOLIO", "REJECT", portfolio.reason_code)
            return
        self._record_funnel("PORTFOLIO", "PASS", portfolio.reason_code)

        pip_value = 0.0001 if "JPY" not in self.p.symbol else 0.01
        stop_distance = technical.stop_pips * pip_value
        tp_distance = technical.take_profit_pips * pip_value
        if stop_distance <= 0 or tp_distance <= 0:
            return

        cash = self._current_balance()
        risk_amount = cash * portfolio.final_risk_percent
        entry_price = float(self.data.close[0])
        if self.simulation_profile.realistic_constraints:
            feasibility = assess_trade_feasibility(
                symbol=self.p.symbol,
                entry_price=entry_price,
                stop_pips=technical.stop_pips,
                risk_amount_usd=risk_amount,
                profile=self.simulation_profile,
            )
            if not feasibility.approved:
                self._record_funnel("FEASIBILITY", "REJECT", feasibility.reason_code)
                self.rejected_signals.append(
                    {
                        "timestamp": self.data.datetime.datetime(0).isoformat(),
                        "symbol": self.p.symbol,
                        "reason_code": feasibility.reason_code,
                        "details": feasibility.details,
                    }
                )
                return
            self._record_funnel("FEASIBILITY", "PASS", feasibility.reason_code)
            size = feasibility.estimated_units
            effective_risk_amount = feasibility.effective_risk_amount_usd
            round_trip_cost_usd = feasibility.round_trip_cost_usd
            estimated_lot = feasibility.estimated_lot
        else:
            self._record_funnel("FEASIBILITY", "BYPASS", "SIMPLIFIED_BROKER_MODEL")
            size = risk_amount / stop_distance
            effective_risk_amount = risk_amount
            round_trip_cost_usd = 0.0
            estimated_lot = 0.0
        if size <= 0:
            return

        timestamp = self.data.datetime.datetime(0).isoformat()

        if technical.direction == "BUY":
            stop_price = entry_price - stop_distance
            limit_price = entry_price + tp_distance
            self.buy_bracket(size=size, stopprice=stop_price, limitprice=limit_price)
        else:
            stop_price = entry_price + stop_distance
            limit_price = entry_price - tp_distance
            self.sell_bracket(size=size, stopprice=stop_price, limitprice=limit_price)

        self._current_risk_amount = effective_risk_amount
        self._current_entry_info = {
            "timestamp": timestamp,
            "direction": technical.direction,
            "regime": regime.regime,
            "round_trip_cost_usd": round_trip_cost_usd,
            "estimated_lot": estimated_lot,
        }

        self.signals.append(
            {
                "timestamp": timestamp,
                "regime": regime.regime,
                "technical": technical.reason_code,
                "portfolio": portfolio.reason_code,
                "mode": self.simulation_profile.mode_id,
            }
        )
        self._record_funnel("ROUTER", "ROUTED", "BACKTEST_ORDER_SUBMITTED")

    def notify_order(self, order: bt.Order) -> None:
        if order.status in [order.Cancelled, order.Margin, order.Rejected]:
            self._current_risk_amount = 0.0

    def notify_trade(self, trade: bt.Trade) -> None:
        if not trade.isclosed:
            return

        risk = self._current_risk_amount
        if self.simulation_profile.realistic_constraints:
            exit_reference_price = max(float(self.data.close[0]), 1e-9)
            gross_pnl_usd = convert_quote_pnl_to_usd(self.p.symbol, float(trade.pnl), exit_reference_price)
            pnl = gross_pnl_usd - float(self._current_entry_info.get("round_trip_cost_usd", 0.0))
            self.simulated_balance_usd = round(self.simulated_balance_usd + pnl, 8)
            self.peak_simulated_balance_usd = max(
                self.peak_simulated_balance_usd,
                self.simulated_balance_usd,
            )
            if self.peak_simulated_balance_usd > 0:
                drawdown_pct = (
                    (self.peak_simulated_balance_usd - self.simulated_balance_usd)
                    / self.peak_simulated_balance_usd
                    * 100.0
                )
                self.max_simulated_drawdown_pct = max(self.max_simulated_drawdown_pct, drawdown_pct)
        else:
            pnl = float(trade.pnl)

        r_multiple = pnl / risk if risk > 0 else 0.0

        self.results.append(
            {
                "timestamp": self._current_entry_info.get("timestamp", ""),
                "direction": self._current_entry_info.get("direction", ""),
                "regime": self._current_entry_info.get("regime", ""),
                "pnl": round(pnl, 4),
                "r_multiple": round(r_multiple, 3),
                "estimated_lot": round(float(self._current_entry_info.get("estimated_lot", 0.0)), 6),
            }
        )
        self._current_risk_amount = 0.0
        self._current_entry_info = {}
