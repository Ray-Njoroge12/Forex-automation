from __future__ import annotations

from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
import pandas as pd

from config_microcapital import apply_agent_threshold_overrides, get_policy_config
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.credentials import load_mt5_credentials_from_env
from core.env_loader import load_runtime_env
from core.indicators import calculate_adx
from core.timeframes import TIMEFRAME_H1, TIMEFRAME_M15

load_runtime_env()
creds = load_mt5_credentials_from_env()
TIMEFRAME_H4 = 16388
SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"]
SELECTIVE = {"GBPUSD", "USDJPY"}
SPREADS = {"EURUSD": 0.00010, "GBPUSD": 0.00012, "USDJPY": 0.012, "AUDUSD": 0.00012, "USDCAD": 0.00013, "USDCHF": 0.00014}
MILD = {"AGENT_THRESHOLDS": {"REGIME": {"adx_no_trade_below": 18.0, "adx_transition_below": 22.0}}}
CANDIDATES = ["baseline", "regime_mild", "rising_adx_relax", "normalvol_rising_relax", "pair_selective_relax", "pair_selective_rising_relax"]


def fetch_df(symbol: str, days: int = 120) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M15, start, end)
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("time").sort_index().rename(columns={"tick_volume": "volume"})[["open", "high", "low", "close", "volume"]]


def make_tf(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


def simulate_trade(df: pd.DataFrame, start_idx: int, signal) -> tuple[float, int]:
    pip = 0.01 if "JPY" in signal.symbol else 0.0001
    entry = float(df.iloc[start_idx]["open"])
    stop_dist = float(signal.stop_pips) * pip
    tp_dist = float(signal.take_profit_pips) * pip
    sl = entry - stop_dist if signal.direction == "BUY" else entry + stop_dist
    tp = entry + tp_dist if signal.direction == "BUY" else entry - tp_dist
    for i in range(start_idx, len(df)):
        row = df.iloc[i]
        hit_sl = float(row["low"]) <= sl if signal.direction == "BUY" else float(row["high"]) >= sl
        hit_tp = float(row["high"]) >= tp if signal.direction == "BUY" else float(row["low"]) <= tp
        if hit_sl:
            return -1.0, i
        if hit_tp:
            return float(signal.risk_reward), i
    last_close = float(df.iloc[-1]["close"])
    r = (last_close - entry) / stop_dist if signal.direction == "BUY" else (entry - last_close) / stop_dist
    return float(r), len(df) - 1


def adx_rising(h1: pd.DataFrame, ts) -> bool:
    vals = h1.loc[:ts, "adx"].dropna().tail(5)
    return len(vals) == 5 and float(vals.iloc[-1] - vals.iloc[0]) >= 1.0


def choose_regime(name: str, sym: str, base, relaxed, h1: pd.DataFrame, ts):
    if name == "baseline":
        return base
    if name == "regime_mild":
        return relaxed
    rising = adx_rising(h1, ts)
    if name == "rising_adx_relax":
        return relaxed if rising else base
    if name == "normalvol_rising_relax":
        return relaxed if rising and relaxed.volatility_state == "NORMAL" else base
    if name == "pair_selective_relax":
        return relaxed if sym in SELECTIVE else base
    if name == "pair_selective_rising_relax":
        return relaxed if sym in SELECTIVE and rising else base
    return base


if not mt5.initialize(login=creds.login, password=creds.password, server=creds.server):
    print({"error": mt5.last_error()}, flush=True)
    raise SystemExit(2)

try:
    data = {}
    for sym in SYMBOLS:
        m15 = fetch_df(sym, 120)
        h1 = make_tf(m15, "1h")
        h4 = make_tf(m15, "4h")
        h1 = h1.copy(); h1["adx"] = calculate_adx(h1, 14)
        data[sym] = {"m15": m15, "h1": h1, "h4": h4}
        print({"dataset": sym, "bars": len(m15)}, flush=True)

    base_policy = get_policy_config("core_srs")
    relaxed_policy = apply_agent_threshold_overrides(base_policy, MILD)
    results = {}
    for name in CANDIDATES:
        total_trades = total_wins = 0
        total_r = 0.0
        per_symbol = {}
        for sym in SYMBOLS:
            m15, h1, h4 = data[sym]["m15"], data[sym]["h1"], data[sym]["h4"]
            current_ts = m15.index[399]
            def fetch(_symbol, timeframe, candles):
                frame = m15 if timeframe == TIMEFRAME_M15 else h1 if timeframe == TIMEFRAME_H1 else h4 if timeframe == TIMEFRAME_H4 else pd.DataFrame()
                return frame.loc[:current_ts].tail(candles).copy()
            base_agent = RegimeAgent(sym, fetch, policy=base_policy)
            relaxed_agent = RegimeAgent(sym, fetch, policy=relaxed_policy)
            tech_agent = TechnicalAgent(sym, fetch, fetch_spread=lambda _s, s=sym: SPREADS[s], policy=base_policy)
            trades = wins = regime_pass = tech_pass = 0
            r_sum = 0.0; end = 400
            while end < len(m15) - 1:
                current_ts = m15.index[end - 1]
                regime = choose_regime(name, sym, base_agent.evaluate(TIMEFRAME_H1), relaxed_agent.evaluate(TIMEFRAME_H1), h1, current_ts)
                if regime.regime not in {"TRENDING_BULL", "TRENDING_BEAR"}:
                    end += 1; continue
                regime_pass += 1
                signal = tech_agent.evaluate(regime, TIMEFRAME_M15, TIMEFRAME_H1, TIMEFRAME_H4)
                if signal is None:
                    end += 1; continue
                tech_pass += 1; trades += 1
                r_mult, exit_idx = simulate_trade(m15, end, signal)
                wins += 1 if r_mult > 0 else 0; r_sum += r_mult; end = max(exit_idx + 1, end + 1)
            total_trades += trades; total_wins += wins; total_r += r_sum
            per_symbol[sym] = {"trades": trades, "win_rate_pct": round((wins / trades * 100.0) if trades else 0.0, 2), "avg_r": round((r_sum / trades) if trades else 0.0, 3), "regime_pass": regime_pass, "tech_pass": tech_pass}
        results[name] = {"trades": total_trades, "win_rate_pct": round((total_wins / total_trades * 100.0) if total_trades else 0.0, 2), "avg_r": round((total_r / total_trades) if total_trades else 0.0, 3), "per_symbol": per_symbol}
        print({"candidate": name, **results[name]}, flush=True)

    base = results["baseline"]
    print("ROUND2_DELTAS", flush=True)
    for name in CANDIDATES[1:]:
        r = results[name]
        print({"candidate": name, "delta_trades": r["trades"] - base["trades"], "delta_win_rate_pct": round(r["win_rate_pct"] - base["win_rate_pct"], 2), "delta_avg_r": round(r["avg_r"] - base["avg_r"], 3)}, flush=True)
finally:
    mt5.shutdown()

