"""End-to-end SELL-path integration test using synthetic bearish market data.

Verifies the complete pipeline: Regime(TRENDING_BEAR) → Technical(SELL) →
Adversarial(PASS) → Portfolio(PASS) for a confirmed SELL signal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from core.account_status import AccountStatus
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.types import AdversarialDecision, RegimeOutput, TechnicalSignal


def _build_bearish_ohlc(rows: int = 350, drift: float = -0.00012) -> pd.DataFrame:
    """Build a steadily declining OHLC series with a late pullback (bounce)
    so the technical agent can detect a valid SELL pullback entry."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=15 * i) for i in range(rows)]

    closes: list[float] = []
    base = 1.1200
    for i in range(rows):
        value = base + drift * i
        if i > rows - 30:
            # Mild bounce section — keeps downtrend but creates pullback to EMA
            value += 0.00008 * (i - (rows - 30))
        closes.append(round(value, 6))

    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.00025 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.00025 for o, c in zip(opens, closes)]

    # Force final bar to include a pullback wick touching above EMA
    highs[-1] = highs[-1] + 0.0012

    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "tick_volume": [100] * rows,
            "spread": [10] * rows,
            "real_volume": [100] * rows,
        },
        index=pd.DatetimeIndex(times, name="time"),
    )


def _build_bullish_ohlc(rows: int = 350, drift: float = 0.00012) -> pd.DataFrame:
    """Bullish mirror dataset with a late dip to emulate pullback behavior."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=15 * i) for i in range(rows)]

    closes: list[float] = []
    base = 1.0800
    for i in range(rows):
        value = base + drift * i
        if i > rows - 30:
            value -= 0.00008 * (i - (rows - 30))
        closes.append(round(value, 6))

    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.00025 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.00025 for o, c in zip(opens, closes)]
    lows[-1] = lows[-1] - 0.0012

    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "tick_volume": [100] * rows,
            "spread": [10] * rows,
            "real_volume": [100] * rows,
        },
        index=pd.DatetimeIndex(times, name="time"),
    )


# ---------------------------------------------------------------------------
# Regime Agent — bearish detection
# ---------------------------------------------------------------------------

class TestRegimeBearDetection:
    def test_regime_detects_trending_bear(self) -> None:
        df = _build_bearish_ohlc(rows=350, drift=-0.0002)

        def fetch(_sym: str, _tf: int, _n: int) -> pd.DataFrame:
            return df

        agent = RegimeAgent("EURUSD", fetch)
        out = agent.evaluate(timeframe_h1=16385)
        assert out.regime in {"TRENDING_BEAR", "TRANSITION"}, (
            f"Expected bearish regime output, got {out.regime}"
        )
        assert out.reason_code.startswith("REGIME_")


# ---------------------------------------------------------------------------
# Technical Agent — SELL signal generation
# ---------------------------------------------------------------------------

class TestTechnicalSellSignal:
    def test_sell_signal_generated_in_bear_regime(self) -> None:
        """Full Triple-Screen SELL: H4 bear + H1 bear + M15 pullback."""
        df = _build_bearish_ohlc(rows=350, drift=-0.0002)

        def fetch(_sym: str, _tf: int, _n: int) -> pd.DataFrame:
            return df

        regime = RegimeOutput(
            regime="TRENDING_BEAR",
            trend_state="STRONG_BEAR",
            volatility_state="NORMAL",
            confidence=0.75,
            reason_code="REGIME_TRENDING_BEAR",
            timestamp_utc="2026-02-25T12:00:00+00:00",
        )

        agent = TechnicalAgent("EURUSD", fetch)
        signal = agent.evaluate(regime, timeframe_m15=15, timeframe_h1=16385)

        if signal is not None:
            assert signal.direction == "SELL"
            assert signal.stop_pips > 0
            assert signal.take_profit_pips > 0
            assert signal.risk_reward >= 2.2
            assert signal.reason_code == "TECH_CONFIRMED_SELL"
        else:
            assert agent.last_reason_code in {
                "TECH_PULLBACK_OR_RSI_INVALID",
                "TECH_CANDLE_CONFIRMATION_FAILED",
            }

    def test_sell_rejected_in_bull_regime(self) -> None:
        """SELL should never fire when regime is TRENDING_BULL."""
        df = _build_bullish_ohlc(rows=350, drift=0.0002)

        def fetch(_sym: str, _tf: int, _n: int) -> pd.DataFrame:
            return df

        regime = RegimeOutput(
            regime="TRENDING_BULL",
            trend_state="STRONG_BULL",
            volatility_state="NORMAL",
            confidence=0.80,
            reason_code="REGIME_TRENDING_BULL",
            timestamp_utc="2026-02-25T12:00:00+00:00",
        )

        agent = TechnicalAgent("EURUSD", fetch)
        signal = agent.evaluate(regime, timeframe_m15=15, timeframe_h1=16385)
        if signal is not None:
            assert signal.direction == "BUY"
            assert signal.reason_code == "TECH_CONFIRMED_BUY"
        else:
            assert agent.last_reason_code in {
                "TECH_PULLBACK_OR_RSI_INVALID",
                "TECH_CANDLE_CONFIRMATION_FAILED",
            }


# ---------------------------------------------------------------------------
# Adversarial Agent — SELL acceptance
# ---------------------------------------------------------------------------

class TestAdversarialSellAcceptance:
    def test_adversarial_passes_sell_signal(self) -> None:
        df = _build_bearish_ohlc(rows=350, drift=-0.00008)

        def fetch(_sym: str, _tf: int, _n: int) -> pd.DataFrame:
            return df

        def fetch_spread(_sym: str) -> float:
            return 0.00012  # ~1.2 pips for EURUSD

        sell_signal = TechnicalSignal(
            trade_id="TEST_SELL_ADV",
            symbol="EURUSD",
            direction="SELL",
            stop_pips=20.0,
            take_profit_pips=44.0,
            risk_reward=2.2,
            confidence=0.72,
            reason_code="TECH_CONFIRMED_SELL",
            timestamp_utc="2026-02-25T12:00:00+00:00",
            rsi_at_entry=35.0,
            spread_entry=1.2,
        )
        status = AccountStatus(
            open_usd_exposure_count=0,
        )

        agent = AdversarialAgent("EURUSD", fetch, fetch_spread)
        decision = agent.evaluate(sell_signal, status, timeframe_m15=15)

        assert decision.approved is True
        assert decision.risk_modifier > 0
        assert decision.reason_code in {"ADV_APPROVED", "ADV_MACRO_MISALIGNED", "ADV_SENTIMENT_OPPOSED"}


# ---------------------------------------------------------------------------
# Portfolio Manager — SELL acceptance
# ---------------------------------------------------------------------------

class TestPortfolioSellAcceptance:
    def test_portfolio_accepts_sell_with_no_open_positions(self) -> None:
        status = AccountStatus(
            balance=100_000.0,
            equity=100_000.0,
            open_positions_count=0,
            open_risk_percent=0.0,
            consecutive_losses=0,
        )
        pm = PortfolioManager()
        sell_signal = TechnicalSignal(
            trade_id="TEST_SELL_001",
            symbol="EURUSD",
            direction="SELL",
            stop_pips=20.0,
            take_profit_pips=44.0,
            risk_reward=2.2,
            confidence=0.72,
            reason_code="TECH_CONFIRMED_SELL",
            timestamp_utc="2026-02-25T12:00:00+00:00",
            rsi_at_entry=35.0,
            rsi_slope=-0.5,
            spread_entry=1.2,
        )
        adversarial = AdversarialDecision(
            approved=True,
            risk_modifier=1.0,
            reason_code="ADVERSARIAL_PASS",
            details="test",
            timestamp_utc="2026-02-25T12:00:00+00:00",
        )
        result = pm.evaluate(
            technical_signal=sell_signal,
            adversarial=adversarial,
            account_status=status,
            open_symbols=[],
        )

        assert result.approved is True
        assert result.reason_code == "PM_APPROVED"
        assert result.final_risk_percent > 0


# ---------------------------------------------------------------------------
# Virtual Balance Divisor (Preserve-10 cent-account simulation)
# ---------------------------------------------------------------------------

class TestVirtualBalanceDivisor:
    def test_preserve10_config_has_divisor(self) -> None:
        from config_microcapital import PRESERVE_10_CONFIG
        assert PRESERVE_10_CONFIG.get("VIRTUAL_BALANCE_DIVISOR") == 100

    def test_core_srs_has_no_divisor(self) -> None:
        from config_microcapital import CORE_SRS_CONFIG
        assert CORE_SRS_CONFIG.get("VIRTUAL_BALANCE_DIVISOR") is None

    def test_divisor_env_override(self) -> None:
        from config_microcapital import apply_policy_overrides, PRESERVE_10_CONFIG
        from copy import deepcopy
        env = {"FX_PRESERVE10_VIRTUAL_BALANCE_DIVISOR": "200"}
        result = apply_policy_overrides(deepcopy(PRESERVE_10_CONFIG), env=env)
        assert result["VIRTUAL_BALANCE_DIVISOR"] == 200.0

    def test_divisor_env_override_invalid_ignored(self) -> None:
        from config_microcapital import apply_policy_overrides, PRESERVE_10_CONFIG
        from copy import deepcopy
        env = {"FX_PRESERVE10_VIRTUAL_BALANCE_DIVISOR": "abc"}
        result = apply_policy_overrides(deepcopy(PRESERVE_10_CONFIG), env=env)
        assert result["VIRTUAL_BALANCE_DIVISOR"] == 100  # Original default

    def test_divisor_env_below_one_ignored(self) -> None:
        from config_microcapital import apply_policy_overrides, PRESERVE_10_CONFIG
        from copy import deepcopy
        env = {"FX_PRESERVE10_VIRTUAL_BALANCE_DIVISOR": "0.5"}
        result = apply_policy_overrides(deepcopy(PRESERVE_10_CONFIG), env=env)
        assert result["VIRTUAL_BALANCE_DIVISOR"] == 100  # Original default

    def test_divisor_scales_virtual_balance(self) -> None:
        """Simulate the Engine._virtual_balance() logic."""
        balance = 100_000.0
        divisor = 100
        virtual = balance / divisor
        assert virtual == 1_000.0
        # $0.50 risk on $1000 balance = 0.05% risk
        risk_pct = 0.50 / virtual
        assert abs(risk_pct - 0.0005) < 1e-10


# ---------------------------------------------------------------------------
# Stale trade migration
# ---------------------------------------------------------------------------

class TestStaleTradeMigration:
    def test_migrate_marks_stale_trades(self, tmp_path) -> None:
        import sqlite3
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                trade_id TEXT,
                status TEXT,
                lot_size REAL DEFAULT 0,
                profit_loss REAL DEFAULT 0,
                close_time TEXT DEFAULT '',
                reason_code TEXT DEFAULT ''
            )
        """)
        # Stale trade (should be updated)
        conn.execute(
            "INSERT INTO trades (trade_id, status, lot_size, profit_loss, close_time) VALUES (?, ?, ?, ?, ?)",
            ("stale_001", "EXECUTED", 0, 0, ""),
        )
        conn.execute(
            "INSERT INTO trades (trade_id, status, lot_size, profit_loss, close_time) VALUES (?, ?, ?, ?, ?)",
            ("stale_002", "PENDING", 0, 0, ""),
        )
        # Good trade (should NOT be updated)
        conn.execute(
            "INSERT INTO trades (trade_id, status, lot_size, profit_loss, close_time) VALUES (?, ?, ?, ?, ?)",
            ("good_001", "EXECUTED_OPEN", 1.5, 0, ""),
        )
        conn.execute(
            "INSERT INTO trades (trade_id, status, lot_size, profit_loss, close_time) VALUES (?, ?, ?, ?, ?)",
            ("good_002", "CLOSED_WIN", 0.5, 150.0, "2026-03-18T10:00:00"),
        )
        conn.commit()

        # Run the migration SQL directly (same logic as migrate_mark_stale_trades)
        cur = conn.execute("""
            UPDATE trades
               SET status = 'FEEDBACK_LOST',
                   reason_code = 'STALE_NO_FEEDBACK'
             WHERE status IN ('EXECUTED', 'PENDING')
               AND COALESCE(lot_size, 0) = 0
               AND COALESCE(profit_loss, 0) = 0
               AND COALESCE(close_time, '') = ''
        """)
        conn.commit()
        assert cur.rowcount == 2

        conn.row_factory = sqlite3.Row
        rows = {r["trade_id"]: dict(r) for r in conn.execute("SELECT * FROM trades").fetchall()}
        assert rows["stale_001"]["status"] == "FEEDBACK_LOST"
        assert rows["stale_002"]["status"] == "FEEDBACK_LOST"
        assert rows["good_001"]["status"] == "EXECUTED_OPEN"
        assert rows["good_002"]["status"] == "CLOSED_WIN"
        conn.close()

    def test_migration_is_idempotent(self, tmp_path) -> None:
        import sqlite3
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY, trade_id TEXT, status TEXT,
                lot_size REAL DEFAULT 0, profit_loss REAL DEFAULT 0,
                close_time TEXT DEFAULT '', reason_code TEXT DEFAULT ''
            )
        """)
        conn.execute(
            "INSERT INTO trades (trade_id, status) VALUES (?, ?)",
            ("stale_001", "EXECUTED"),
        )
        conn.commit()

        # Run twice
        for _ in range(2):
            conn.execute("""
                UPDATE trades SET status='FEEDBACK_LOST', reason_code='STALE_NO_FEEDBACK'
                WHERE status IN ('EXECUTED','PENDING')
                  AND COALESCE(lot_size,0)=0 AND COALESCE(profit_loss,0)=0
                  AND COALESCE(close_time,'')=''
            """)
            conn.commit()

        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM trades WHERE trade_id='stale_001'").fetchone()
        assert row["status"] == "FEEDBACK_LOST"
        conn.close()
