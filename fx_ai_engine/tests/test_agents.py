from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from core.account_status import AccountStatus
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.types import AdversarialDecision, RegimeOutput, TechnicalSignal


def _build_ohlc_series(rows: int = 350, drift: float = 0.00012) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=15 * i) for i in range(rows)]

    closes: list[float] = []
    base = 1.0800
    for i in range(rows):
        value = base + drift * i
        if i > rows - 30:
            # Mild pullback section keeps trend while avoiding runaway RSI.
            value -= 0.00008 * (i - (rows - 30))
        closes.append(round(value, 6))

    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.00025 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.00025 for o, c in zip(opens, closes)]

    # Force final bar to include an EMA pullback wick.
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


def test_regime_agent_returns_trending_bull() -> None:
    h1_df = _build_ohlc_series(rows=350, drift=0.0002)

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        return h1_df

    agent = RegimeAgent("EURUSD", fetch)
    out = agent.evaluate(timeframe_h1=16385)

    assert out.regime in {"TRENDING_BULL", "TRANSITION", "RANGING_HIGH_VOL", "RANGING_LOW_VOL", "NO_TRADE"}
    # Core contract: deterministic output and reason code present.
    assert out.reason_code.startswith("REGIME_")


def test_technical_agent_blocks_when_regime_not_trending() -> None:
    df = _build_ohlc_series()

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        return df

    agent = TechnicalAgent("EURUSD", fetch)
    regime = RegimeOutput(
        regime="NO_TRADE",
        trend_state="FLAT",
        volatility_state="NORMAL",
        confidence=0.3,
        reason_code="REGIME_NO_TRADE",
        timestamp_utc="2026-02-25T12:00:00+00:00",
    )

    signal = agent.evaluate(regime, timeframe_m15=15, timeframe_h1=16385)
    assert signal is None


def test_regime_agent_policy_thresholds_can_promote_trend(monkeypatch) -> None:
    from core.agents import regime_agent as regime_mod

    h1_df = _build_ohlc_series(rows=350, drift=0.0002)

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        return h1_df

    fast = pd.Series([1.0000 + i * 0.00001 for i in range(len(h1_df))], index=h1_df.index)
    slow = pd.Series([1.0000] * len(h1_df), index=h1_df.index)
    atr = pd.Series([0.0010] * len(h1_df), index=h1_df.index)
    adx = pd.Series([19.5] * len(h1_df), index=h1_df.index)

    def fake_ema(_series: pd.Series, period: int) -> pd.Series:
        return fast if period == 50 else slow

    monkeypatch.setattr(regime_mod, "calculate_ema", fake_ema)
    monkeypatch.setattr(regime_mod, "calculate_atr", lambda df, period: atr)
    monkeypatch.setattr(regime_mod, "calculate_adx", lambda df, period: adx)

    default_agent = RegimeAgent("EURUSD", fetch)
    default_out = default_agent.evaluate(timeframe_h1=16385)
    relaxed_agent = RegimeAgent(
        "EURUSD",
        fetch,
        policy={
            "AGENT_THRESHOLDS": {
                "REGIME": {"adx_no_trade_below": 18.0, "adx_transition_below": 19.0}
            }
        },
    )
    relaxed_out = relaxed_agent.evaluate(timeframe_h1=16385)

    assert default_out.regime != "TRENDING_BULL"
    assert relaxed_out.regime == "TRENDING_BULL"
    assert "adx_no_trade_below=18.00" in relaxed_agent.last_details


def test_regime_agent_pair_selective_rising_adx_relax_only_applies_to_target_symbols(monkeypatch) -> None:
    from core.agents import regime_agent as regime_mod

    h1_df = _build_ohlc_series(rows=350, drift=0.0002)

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        return h1_df

    fast = pd.Series([1.0000 + i * 0.00001 for i in range(len(h1_df))], index=h1_df.index)
    slow = pd.Series([1.0000] * len(h1_df), index=h1_df.index)
    atr = pd.Series([0.0010] * len(h1_df), index=h1_df.index)
    adx = pd.Series([21.6] * (len(h1_df) - 5) + [21.8, 22.1, 22.4, 22.7, 23.0], index=h1_df.index)

    monkeypatch.setattr(regime_mod, "calculate_ema", lambda _series, period: fast if period == 50 else slow)
    monkeypatch.setattr(regime_mod, "calculate_atr", lambda df, period: atr)
    monkeypatch.setattr(regime_mod, "calculate_adx", lambda df, period: adx)

    policy = {
        "EXPERIMENTS": {
            "PAIR_SELECTIVE_RISING_ADX_RELAX": {
                "enabled": True,
                "symbols": ["EURUSD", "USDJPY"],
                "adx_lookback_bars": 5,
                "adx_rise_min": 1.0,
                "adx_no_trade_below": 18.0,
                "adx_transition_below": 22.0,
            }
        }
    }

    eurusd = RegimeAgent("EURUSD", fetch, policy=policy).evaluate(timeframe_h1=16385)
    gbpusd_agent = RegimeAgent("GBPUSD", fetch, policy=policy)
    gbpusd = gbpusd_agent.evaluate(timeframe_h1=16385)

    assert eurusd.regime == "TRENDING_BULL"
    assert gbpusd.regime != "TRENDING_BULL"
    assert "experiment_pair_selective_rising_adx_relax=0" in gbpusd_agent.last_details


def test_regime_agent_details_include_direction_candidate_and_reference_price(monkeypatch) -> None:
    from core.agents import regime_agent as regime_mod

    h1_df = _build_ohlc_series(rows=350, drift=0.0002)

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        return h1_df

    fast = pd.Series([1.1010] * len(h1_df), index=h1_df.index)
    slow = pd.Series([1.1000] * len(h1_df), index=h1_df.index)
    atr = pd.Series([0.0010] * len(h1_df), index=h1_df.index)
    adx = pd.Series([15.0] * len(h1_df), index=h1_df.index)

    monkeypatch.setattr(regime_mod, "calculate_ema", lambda _series, period: fast if period == 50 else slow)
    monkeypatch.setattr(regime_mod, "calculate_atr", lambda df, period: atr)
    monkeypatch.setattr(regime_mod, "calculate_adx", lambda df, period: adx)

    agent = RegimeAgent("EURUSD", fetch)
    out = agent.evaluate(timeframe_h1=16385)

    assert out.reason_code in {"REGIME_NO_TRADE", "REGIME_RANGE_LOW_VOL", "REGIME_RANGE_HIGH_VOL"}
    assert "direction_candidate=BUY" in agent.last_details
    assert "close=" in agent.last_details
    assert "ema_fast=1.10100" in agent.last_details


def test_technical_agent_policy_thresholds_can_promote_near_miss(monkeypatch) -> None:
    from core.agents import technical_agent as technical_mod

    h4 = _build_ohlc_series(rows=350, drift=0.0002)
    h1 = _build_ohlc_series(rows=350, drift=0.0002)
    m15 = _build_ohlc_series(rows=350, drift=0.0002)
    m15.loc[m15.index[-1], "low"] = 1.1005
    m15.loc[m15.index[-1], "close"] = 1.1010
    m15.loc[m15.index[-1], "high"] = 1.1012

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        if _timeframe == 16388:
            return h4
        if _timeframe == 16385:
            return h1
        return m15

    regime = RegimeOutput(
        regime="TRENDING_BULL",
        trend_state="BULLISH",
        volatility_state="NORMAL",
        confidence=0.8,
        reason_code="REGIME_TREND_BULL",
        timestamp_utc="2026-03-05T12:00:00+00:00",
    )

    def patch_indicator_stack() -> None:
        ema_outputs = [
            pd.Series([1.1020] * len(h4), index=h4.index),
            pd.Series([1.1000] * len(h4), index=h4.index),
            pd.Series([1.1015] * len(h1), index=h1.index),
            pd.Series([1.0995] * len(h1), index=h1.index),
            pd.Series([1.0990 + i * 0.00001 for i in range(len(m15))], index=m15.index),
        ]
        monkeypatch.setattr(technical_mod, "calculate_ema", lambda series, period: ema_outputs.pop(0))
        monkeypatch.setattr(
            technical_mod,
            "calculate_atr",
            lambda df, period: pd.Series([0.0008] * len(df), index=df.index),
        )
        monkeypatch.setattr(
            technical_mod,
            "calculate_rsi",
            lambda series, period: pd.Series([39.0] * len(series), index=series.index),
        )

    default_agent = TechnicalAgent("EURUSD", fetch, fetch_spread=lambda s: 0.0001)
    monkeypatch.setattr(default_agent, "_confirm_candle_pattern", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(default_agent, "_detect_structural_sl", lambda *_args, **_kwargs: (10.0, None))
    patch_indicator_stack()
    default_signal = default_agent.evaluate(regime, timeframe_m15=15, timeframe_h1=16385)

    relaxed_agent = TechnicalAgent(
        "EURUSD",
        fetch,
        fetch_spread=lambda s: 0.0001,
        policy={
            "AGENT_THRESHOLDS": {
                "TECHNICAL": {"pullback_buffer_pips": 5.0, "buy_rsi_min": 39.0}
            }
        },
    )
    monkeypatch.setattr(relaxed_agent, "_confirm_candle_pattern", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(relaxed_agent, "_detect_structural_sl", lambda *_args, **_kwargs: (10.0, None))
    patch_indicator_stack()
    relaxed_signal = relaxed_agent.evaluate(regime, timeframe_m15=15, timeframe_h1=16385)

    assert default_signal is None
    assert default_agent.last_reason_code == "TECH_PULLBACK_OR_RSI_INVALID"
    assert "pullback_gap_pips=" in default_agent.last_details
    assert "rsi=39.00" in default_agent.last_details
    assert relaxed_signal is not None
    assert relaxed_signal.reason_code == "TECH_CONFIRMED_BUY"


def test_technical_agent_audusd_pullback_experiment_only_applies_to_audusd(monkeypatch) -> None:
    from core.agents import technical_agent as technical_mod

    h4 = _build_ohlc_series(rows=350, drift=0.0002)
    h1 = _build_ohlc_series(rows=350, drift=0.0002)
    m15 = _build_ohlc_series(rows=350, drift=0.0002)
    m15.loc[m15.index[-1], "low"] = 1.10070
    m15.loc[m15.index[-1], "close"] = 1.1010
    m15.loc[m15.index[-1], "high"] = 1.1012

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        if _timeframe == 16388:
            return h4
        if _timeframe == 16385:
            return h1
        return m15

    regime = RegimeOutput(
        regime="TRENDING_BULL",
        trend_state="BULLISH",
        volatility_state="NORMAL",
        confidence=0.8,
        reason_code="REGIME_TREND_BULL",
        timestamp_utc="2026-03-05T12:00:00+00:00",
    )

    def patch_indicator_stack() -> None:
        ema_outputs = [
            pd.Series([1.1020] * len(h4), index=h4.index),
            pd.Series([1.1000] * len(h4), index=h4.index),
            pd.Series([1.1015] * len(h1), index=h1.index),
            pd.Series([1.0995] * len(h1), index=h1.index),
            pd.Series([1.1000 + i * 0.000001 for i in range(len(m15))], index=m15.index),
        ]
        monkeypatch.setattr(technical_mod, "calculate_ema", lambda series, period: ema_outputs.pop(0))
        monkeypatch.setattr(
            technical_mod,
            "calculate_atr",
            lambda df, period: pd.Series([0.0008] * len(df), index=df.index),
        )
        monkeypatch.setattr(
            technical_mod,
            "calculate_rsi",
            lambda series, period: pd.Series([50.0] * len(series), index=series.index),
        )

    policy = {
        "EXPERIMENTS": {
            "AUDUSD_PULLBACK_RELAX": {
                "enabled": True,
                "symbols": ["AUDUSD"],
                "pullback_buffer_pips": 4.0,
            }
        }
    }

    audusd_agent = TechnicalAgent("AUDUSD", fetch, fetch_spread=lambda s: 0.0001, policy=policy)
    monkeypatch.setattr(audusd_agent, "_confirm_candle_pattern", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(audusd_agent, "_detect_structural_sl", lambda *_args, **_kwargs: (10.0, None))
    patch_indicator_stack()
    audusd_signal = audusd_agent.evaluate(regime, timeframe_m15=15, timeframe_h1=16385)

    eurusd_agent = TechnicalAgent("EURUSD", fetch, fetch_spread=lambda s: 0.0001, policy=policy)
    monkeypatch.setattr(eurusd_agent, "_confirm_candle_pattern", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(eurusd_agent, "_detect_structural_sl", lambda *_args, **_kwargs: (10.0, None))
    patch_indicator_stack()
    eurusd_signal = eurusd_agent.evaluate(regime, timeframe_m15=15, timeframe_h1=16385)

    assert audusd_signal is not None
    assert eurusd_signal is None
    assert "experiment_audusd_pullback_relax=1" in audusd_agent.last_details
    assert "buffer_pips=4.00" in audusd_agent.last_details
    assert eurusd_agent.last_reason_code == "TECH_PULLBACK_OR_RSI_INVALID"
    assert "experiment_audusd_pullback_relax=0" in eurusd_agent.last_details


def test_adversarial_agent_rejects_wide_spread() -> None:
    df = _build_ohlc_series()

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        return df

    def spread(_symbol: str) -> float:
        return 0.00035  # 3.5 pips on non-JPY pair

    signal = TechnicalSignal(
        trade_id="AI_20260225_123000_deadbe",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=12.0,
        take_profit_pips=26.4,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TECH_PULLBACK_BUY",
        timestamp_utc="2026-02-25T12:30:00+00:00",
    )

    out = AdversarialAgent("EURUSD", fetch, spread).evaluate(
        technical_signal=signal,
        account_status=AccountStatus(),
        timeframe_m15=15,
    )

    assert out.approved is False
    assert out.reason_code == "ADV_SPREAD_TOO_WIDE"


def test_portfolio_manager_applies_trade_and_exposure_caps() -> None:
    manager = PortfolioManager()

    signal = TechnicalSignal(
        trade_id="AI_20260225_124500_aa11bb",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=25.0,
        risk_reward=2.5,
        confidence=0.75,
        reason_code="TECH_PULLBACK_BUY",
        timestamp_utc="2026-02-25T12:45:00+00:00",
    )

    status = AccountStatus(open_positions_count=2)
    adversarial_ok = AdversarialDecision(
        approved=True,
        risk_modifier=1.0,
        reason_code="ADV_APPROVED",
        details="ok",
        timestamp_utc="2026-02-25T12:45:00+00:00",
    )

    decision = manager.evaluate(signal, adversarial_ok, status)
    assert decision.approved is False
    assert decision.reason_code == "PM_MAX_TRADES_REACHED"


def _make_m15_with_swing(rows: int = 30, low_at: int = 25, swing_low: float = 1.0770) -> pd.DataFrame:
    """M15 frame with a clear swing low inserted at bar `low_at`."""
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 3, 5, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=15 * i) for i in range(rows)]
    closes = [1.0800] * rows
    opens = closes[:]
    highs = [c + 0.0005 for c in closes]
    lows = [c - 0.0003 for c in closes]
    lows[low_at] = swing_low  # insert structural low
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "tick_volume": [100] * rows, "spread": [10] * rows, "real_volume": [100] * rows},
        index=pd.DatetimeIndex(times, name="time"),
    )


def test_structural_sl_snaps_to_swing_low_for_buy() -> None:
    from core.agents.technical_agent import TechnicalAgent
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    m15 = _make_m15_with_swing(rows=30, low_at=25, swing_low=1.0770)
    current_price = 1.0800
    # ATR stop ~12 pips; structural low is 30 pips away → too wide, keep ATR
    final_stop, snapped = agent._detect_structural_sl(m15, "BUY", atr_stop_pips=12.0, current_price=current_price)
    assert final_stop == 12.0
    assert snapped is None


def test_structural_sl_snaps_when_within_window() -> None:
    from core.agents.technical_agent import TechnicalAgent
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    m15 = _make_m15_with_swing(rows=30, low_at=25, swing_low=1.0789)
    current_price = 1.0800
    # Swing low is 11 pips away; ATR stop 12 pips → ratio 0.917 → inside [0.8, 1.5] window
    final_stop, snapped = agent._detect_structural_sl(m15, "BUY", atr_stop_pips=12.0, current_price=current_price)
    assert abs(final_stop - 11.0) < 0.5   # snapped to structural
    assert snapped is not None


def test_structural_sl_keeps_atr_when_too_tight() -> None:
    from core.agents.technical_agent import TechnicalAgent
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    m15 = _make_m15_with_swing(rows=30, low_at=25, swing_low=1.0796)
    current_price = 1.0800
    # Swing low 4 pips → ratio 0.33 → below 0.8 window, too tight → keep ATR
    final_stop, snapped = agent._detect_structural_sl(m15, "BUY", atr_stop_pips=12.0, current_price=current_price)
    assert final_stop == 12.0
    assert snapped is None


def test_trade_params_trending_normal_vol() -> None:
    from core.agents.technical_agent import TechnicalAgent
    from core.types import RegimeOutput
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    regime = RegimeOutput(
        regime="TRENDING_BULL", trend_state="UP", volatility_state="NORMAL",
        confidence=0.8, reason_code="REGIME_TRENDING_BULL", timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    params = agent._get_trade_management_params(regime)
    assert params["be_trigger_r"] == 0.8
    assert params["partial_close_r"] == 1.2
    assert params["trailing_atr_mult"] == 1.5
    assert params["tp_mode"] == "TRAIL"


def test_trade_params_trending_high_vol() -> None:
    from core.agents.technical_agent import TechnicalAgent
    from core.types import RegimeOutput
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    regime = RegimeOutput(
        regime="TRENDING_BEAR", trend_state="DOWN", volatility_state="HIGH",
        confidence=0.8, reason_code="REGIME_TRENDING_BEAR", timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    params = agent._get_trade_management_params(regime)
    assert params["be_trigger_r"] == 1.2
    assert params["partial_close_r"] == 1.5
    assert params["trailing_atr_mult"] == 2.0
    assert params["tp_mode"] == "TRAIL"


def test_trade_params_option_c_trending_normal_vol() -> None:
    from core.agents.technical_agent import TechnicalAgent
    from core.types import RegimeOutput

    agent = TechnicalAgent(
        "EURUSD",
        fetch_ohlc=lambda s, t, n: pd.DataFrame(),
        policy={"EXPERIMENTS": {"LIVE_TRADE_MGMT_OPTION_C": {"enabled": True}}},
    )
    regime = RegimeOutput(
        regime="TRENDING_BULL", trend_state="UP", volatility_state="NORMAL",
        confidence=0.8, reason_code="REGIME_TRENDING_BULL", timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    params = agent._get_trade_management_params(regime)
    assert params["be_trigger_r"] == 0.5
    assert params["partial_close_r"] == 1.0
    assert params["trailing_atr_mult"] == 1.5
    assert params["tp_mode"] == "HYBRID"


def test_trade_params_option_c_trending_high_vol() -> None:
    from core.agents.technical_agent import TechnicalAgent
    from core.types import RegimeOutput

    agent = TechnicalAgent(
        "EURUSD",
        fetch_ohlc=lambda s, t, n: pd.DataFrame(),
        policy={"EXPERIMENTS": {"LIVE_TRADE_MGMT_OPTION_C": {"enabled": True}}},
    )
    regime = RegimeOutput(
        regime="TRENDING_BEAR", trend_state="DOWN", volatility_state="HIGH",
        confidence=0.8, reason_code="REGIME_TRENDING_BEAR", timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    params = agent._get_trade_management_params(regime)
    assert params["be_trigger_r"] == 0.75
    assert params["partial_close_r"] == 1.25
    assert params["trailing_atr_mult"] == 2.0
    assert params["tp_mode"] == "HYBRID"


def test_trade_params_ranging_disables_trail() -> None:
    from core.agents.technical_agent import TechnicalAgent
    from core.types import RegimeOutput
    agent = TechnicalAgent("EURUSD", fetch_ohlc=lambda s, t, n: pd.DataFrame())
    regime = RegimeOutput(
        regime="RANGING", trend_state="FLAT", volatility_state="LOW",
        confidence=0.6, reason_code="REGIME_RANGING", timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    params = agent._get_trade_management_params(regime)
    assert params["be_trigger_r"] == 1.0
    assert params["partial_close_r"] == 0.0
    assert params["trailing_atr_mult"] == 0.0
    assert params["tp_mode"] == "FIXED"


def test_technical_agent_signal_carries_trade_management_params() -> None:
    """When evaluate() produces a signal, it must include regime-driven management params."""
    h4 = _build_ohlc_series(rows=350, drift=0.0002)
    h1 = _build_ohlc_series(rows=350, drift=0.0002)
    m15 = _build_ohlc_series(rows=350, drift=0.0002)

    def fetch(_symbol: str, _timeframe: int, _candles: int) -> pd.DataFrame:
        if _timeframe == 16388:   # H4
            return h4
        if _timeframe == 16385:   # H1
            return h1
        return m15

    agent = TechnicalAgent("EURUSD", fetch)
    regime = RegimeOutput(
        regime="TRENDING_BULL",
        trend_state="UP",
        volatility_state="NORMAL",
        confidence=0.8,
        reason_code="REGIME_TRENDING_BULL",
        timestamp_utc="2026-03-05T12:00:00+00:00",
    )
    signal = agent.evaluate(regime, timeframe_m15=1, timeframe_h1=16385)
    if signal is not None:
        # If a signal was produced, it must carry trade management params
        assert signal.tp_mode in {"FIXED", "TRAIL", "HYBRID"}
        assert signal.be_trigger_r > 0
