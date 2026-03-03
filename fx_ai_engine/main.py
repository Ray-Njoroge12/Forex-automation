from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timezone

from bridge.execution_feedback import ExecutionFeedbackReader
from bridge.signal_router import SignalRouter
from core.account_status import AccountStatus
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.credentials import CredentialsError, load_mt5_credentials_from_env
from core.filters.calendar_filter import CalendarEvent, is_news_blackout, load_calendar
from core.filters.macro_filter import load_rate_differentials
from core.filters.session_filter import get_active_session, is_tradeable_session
from core.sentiment.sentiment_agent import SentimentAgent
from ml.signal_ranker import PREDICT_THRESHOLD, SignalRanker
from core.logging_utils import configure_logging
from core.metrics import init_metrics
from core.mt5_bridge import MT5Connection
from core.observability import init_tracing
from core.risk.hard_risk_engine import HardRiskEngine
from core.risk.reset_scheduler import ResetScheduler
from core.schemas import technical_signal_to_payload
from core.state_sync import update_account_status_from_snapshot
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15
from database.db import (
    initialize_schema,
    insert_account_metrics,
    insert_risk_event,
    insert_trade_proposal,
    migrate_add_ml_feature_columns,
    migrate_add_risk_events,
    migrate_phase8_columns,
    update_trade_execution_result,
)

logger = logging.getLogger("fx_ai_engine.main")

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"]


class Engine:
    def __init__(self, bridge: MT5Connection, tracer, metrics):
        self.bridge = bridge
        self.tracer = tracer
        self.metrics = metrics
        self.account_status = AccountStatus()
        self.reset_scheduler = ResetScheduler()
        self.router = SignalRouter()
        self.feedback = ExecutionFeedbackReader()

        # Load static data files once at startup.
        _data_dir = os.path.join(os.path.dirname(__file__), "data")
        self._calendar_events: list[CalendarEvent] = load_calendar(
            os.path.join(_data_dir, "economic_calendar.json")
        )
        self._rate_diffs = load_rate_differentials(
            os.path.join(_data_dir, "rate_differentials.json")
        )

        # Shared sentiment agent (lazy model load, 15-min cache per symbol).
        # Only active when USE_SENTIMENT=1.
        _sentiment = SentimentAgent()

        self.agents = {
            sym: {
                "regime": RegimeAgent(sym, bridge.fetch_ohlc_data),
                "technical": TechnicalAgent(sym, bridge.fetch_ohlc_data, bridge.get_live_spread),
                "adversarial": AdversarialAgent(
                    sym,
                    bridge.fetch_ohlc_data,
                    bridge.get_live_spread,
                    rate_differentials=self._rate_diffs,
                    sentiment_agent=_sentiment,
                ),
            }
            for sym in SYMBOLS
        }
        self.portfolio_manager = PortfolioManager()
        self.hard_risk = HardRiskEngine()
        self._open_symbols: set[str] = set()

        # ML signal ranker — loads from disk if trained; no-ops gracefully otherwise.
        self.ranker = SignalRanker()
        self.ranker.load()

        self.last_m15_candle: datetime | None = None

    def _update_account_state(self) -> None:
        with self.tracer.start_as_current_span("update_account_state") as span:
            snapshot = self.feedback.read_account_snapshot()
            if snapshot is None:
                snapshot = self.bridge.get_account_snapshot() or {}

            update_account_status_from_snapshot(self.account_status, snapshot)

            if self.reset_scheduler.should_reset_daily():
                self.account_status.daily_loss_percent = 0.0
            if self.reset_scheduler.should_reset_weekly():
                self.account_status.weekly_loss_percent = 0.0

            span.set_attribute("open_positions", self.account_status.open_positions_count)
            span.set_attribute("drawdown_percent", self.account_status.drawdown_percent)

        self.metrics.set_gauge("open_positions", self.account_status.open_positions_count)
        self.metrics.set_gauge("open_risk_percent", self.account_status.open_risk_percent)

    def _consume_feedback(self) -> None:
        with self.tracer.start_as_current_span("consume_feedback"):
            for payload in self.feedback.consume_execution_feedback():
                update_trade_execution_result(payload)
                pnl = float(payload.get("profit_loss", 0.0))
                if pnl < 0:
                    self.account_status.consecutive_losses += 1
                elif pnl > 0:
                    self.account_status.consecutive_losses = 0

    def _is_new_m15_candle(self) -> bool:
        df = self.bridge.fetch_ohlc_data(SYMBOLS[0], TIMEFRAME_M15, 2)
        if df.empty:
            return False
        current = df.index[-1].to_pydatetime().replace(tzinfo=timezone.utc)
        if self.last_m15_candle is None or current > self.last_m15_candle:
            self.last_m15_candle = current
            return True
        return False

    def _evaluate_symbol(self, sym: str) -> None:
        now_utc = datetime.now(timezone.utc)
        if is_news_blackout(sym, now_utc, self._calendar_events):
            insert_risk_event(
                "NEWS_BLACKOUT", "INFO",
                f"symbol={sym} — high-impact event blackout window",
            )
            return

        sym_agents = self.agents[sym]
        regime = sym_agents["regime"].evaluate(TIMEFRAME_H1)
        technical = sym_agents["technical"].evaluate(regime, TIMEFRAME_M15, TIMEFRAME_H1)

        if technical is None:
            return

        adversarial = sym_agents["adversarial"].evaluate(
            technical, self.account_status, TIMEFRAME_M15
        )
        portfolio = self.portfolio_manager.evaluate(
            technical, adversarial, self.account_status,
            open_symbols=list(self._open_symbols),
            regime=regime,
        )

        if not portfolio.approved:
            insert_trade_proposal(
                technical,
                status="REJECTED",
                reason_code=portfolio.reason_code,
                risk_percent=0.0,
                market_regime=regime.regime,
            )
            insert_risk_event("PORTFOLIO_GATE", "WARN", portfolio.details, technical.trade_id)
            self.metrics.inc("trades_rejected")
            return

        risk = self.hard_risk.validate(self.account_status, portfolio.final_risk_percent)
        if not risk.approved:
            insert_trade_proposal(
                technical,
                status="REJECTED",
                reason_code=risk.reason_code,
                risk_percent=0.0,
                market_regime=regime.regime,
            )
            insert_risk_event("HARD_RISK", "BLOCK", risk.details, technical.trade_id)
            self.metrics.inc("risk_blocks")
            self.metrics.inc("trades_rejected")
            return

        # ML ranker gate — only active once a model has been trained.
        active_session = get_active_session(now_utc)
        is_london = 1 if active_session == "london" else 0
        is_ny = 1 if active_session == "newyork" else 0
        rate_diff = self._rate_diffs.get(sym, 0.0)
        ranker_features = {
            "regime_confidence": regime.confidence,
            "rsi": technical.rsi_at_entry,
            "atr_ratio": regime.atr_ratio,
            "spread_pips": technical.spread_entry,
            "is_london_session": float(is_london),
            "is_newyork_session": float(is_ny),
            "rate_differential": rate_diff,
            "stop_pips": technical.stop_pips,
            "risk_reward": technical.risk_reward,
            "direction_buy": 1.0 if technical.direction == "BUY" else 0.0,
        }
        ranker_prob = self.ranker.predict_proba(ranker_features)
        if ranker_prob < PREDICT_THRESHOLD:
            insert_trade_proposal(
                technical,
                status="REJECTED",
                reason_code="ML_RANKER_LOW_PROB",
                risk_percent=0.0,
                market_regime=regime.regime,
            )
            insert_risk_event(
                "ML_RANKER", "INFO",
                f"prob={ranker_prob:.3f} < threshold={PREDICT_THRESHOLD}",
                technical.trade_id,
            )
            self.metrics.inc("trades_rejected")
            return

        # Apply loss-streak throttle from hard risk engine.
        final_risk = round(portfolio.final_risk_percent * risk.risk_throttle_multiplier, 4)

        payload = technical_signal_to_payload(technical, final_risk)
        self.router.send(payload)
        insert_trade_proposal(
            technical,
            status="PENDING",
            reason_code="ROUTED_TO_MT5",
            risk_percent=final_risk,
            market_regime=regime.regime,
            regime_confidence=regime.confidence,
            atr_ratio=regime.atr_ratio,
            is_london_session=is_london,
            is_newyork_session=is_ny,
            rate_differential=rate_diff,
        )
        self._open_symbols.add(sym)
        self.metrics.inc("trades_routed")

    def _decision_cycle(self) -> None:
        with self.tracer.start_as_current_span("decision_cycle"):
            now_utc = datetime.now(timezone.utc)
            if not is_tradeable_session(now_utc):
                insert_risk_event(
                    "SESSION_INACTIVE", "INFO",
                    f"hour={now_utc.hour} UTC — outside London/NY session",
                )
                return

            if self.account_status.open_positions_count == 0:
                self._open_symbols.clear()

            for sym in SYMBOLS:
                self._evaluate_symbol(sym)

    def run(self, mode: str, iterations: int = 0) -> None:
        count = 0
        while True:
            self._update_account_state()

            if self.account_status.is_stale(max_age_seconds=180):
                self.account_status.is_trading_halted = True
                insert_risk_event("STATE_STALE", "BLOCK", "Account state stale")
                self.metrics.inc("state_stale")

            self._consume_feedback()

            if self._is_new_m15_candle() and not self.account_status.is_trading_halted:
                self._decision_cycle()

            insert_account_metrics(self.account_status)
            self.metrics.inc("decision_cycles")

            count += 1
            if mode == "smoke":
                if count >= 1:
                    break
            elif iterations > 0 and count >= iterations:
                break

            time.sleep(5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FX AI Engine")
    parser.add_argument("--mode", choices=["smoke", "demo"], default="smoke")
    parser.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Optional loop iterations for demo mode. 0 means run until stopped.",
    )
    return parser.parse_args()


def main() -> int:
    configure_logging()

    initialize_schema()
    migrate_phase8_columns()
    migrate_add_risk_events()
    migrate_add_ml_feature_columns()

    use_mock = os.getenv("USE_MT5_MOCK") == "1"
    if use_mock:
        creds = None
    else:
        try:
            creds = load_mt5_credentials_from_env()
        except CredentialsError as exc:
            logger.error("Credential error: %s", exc)
            return 1

    tracer = init_tracing(os.getenv("OTEL_SERVICE_NAME", "fx_ai_engine"))
    metrics = init_metrics()

    bridge = MT5Connection(
        creds.login if creds else 0,
        creds.password if creds else "",
        creds.server if creds else "",
    )
    if not bridge.connect():
        logger.error("MT5 connection failed: %s", bridge.last_error)
        return 2

    args = parse_args()

    try:
        engine = Engine(bridge, tracer, metrics)
        engine.run(mode=args.mode, iterations=args.iterations)
    finally:
        bridge.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
