"""Event-driven multi-symbol replay that reuses the live decision and risk code (plan build E3).

Pipeline per decision time (an M15 bar close), identical classes to the live engine:
    RegimeAgent -> TechnicalAgent -> AdversarialAgent -> PortfolioManager -> HardRiskEngine

Two stages:
  1. Candidate generation, per symbol (parallelisable): session filter, regime and
     technical agents on a point-in-time feed. Regime results are cached per
     closed H1 bar because RegimeAgent only reads H1.
  2. Portfolio simulation, chronological across symbols: adversarial agent,
     portfolio manager and hard risk engine against a simulated AccountStatus,
     then fills and EA trade-management emulation.

Timing and price assumptions (stated in every report):
  * Bars are broker BID bars with open-time UTC index; a bar is visible only
    once open + timeframe <= decision time (completed bars only).
  * Market orders fill at the open of the next M15 bar: BUY at ask + slippage,
    SELL at bid - slippage. Ask = bid + modelled spread.
  * Stops and targets set at entry or by management are active from the start
    of each bar. When a bar touches both, the stop is assumed first. Gaps
    through a stop fill at the worse bar open.
  * EA management (break-even, 50% partial close, ATR trail) is applied from
    each bar's favourable extreme and takes effect from the next bar. The EA
    acts per tick, so intrabar sequences can differ.
  * Not modelled: economic-calendar blackouts (no point-in-time calendar
    history), swap/rollover, requotes, partial fills, broker stop levels.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from backtesting.replay_execution import QuoteBar, protective_exit
from core.account_status import AccountStatus
from core.agents.adversarial_agent import AdversarialAgent
from core.agents.portfolio_manager import PortfolioManager
from core.agents.regime_agent import RegimeAgent
from core.agents.technical_agent import TechnicalAgent
from core.filters.session_filter import is_tradeable_session
from core.indicators import calculate_atr
from core.risk.hard_risk_engine import HardRiskEngine
from core.types import RegimeOutput, TechnicalSignal

SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF")
TIMEFRAME_CODES = {"M15": 15, "H1": 16385, "H4": 16388, "D1": 16408}
TIMEFRAME_NAMES = {code: name for name, code in TIMEFRAME_CODES.items()}
TIMEFRAME_SECONDS = {"M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
BAR_COLUMNS = ["open", "high", "low", "close", "volume", "spread_points"]
CONTRACT_SIZE = 100_000.0
EA_BREAK_EVEN_BUFFER_PIPS = 1.0  # FX_Execution input BreakEvenBufferPips
EA_TRAIL_ATR_PERIOD = 14         # FX_Execution input TrailingATRPeriod
LOT_STEP, MIN_LOT, MAX_LOT = 0.01, 0.01, 500.0

# Typical retail ECN spreads (pips) used as a floor because MetaQuotes-Demo
# history often records near-zero spreads. Override per run if needed.
DEFAULT_SPREAD_FLOOR_PIPS = {
    "EURUSD": 0.8, "GBPUSD": 1.2, "USDJPY": 1.0, "AUDUSD": 1.0, "USDCAD": 1.5, "USDCHF": 1.5,
}


def pip_size(symbol: str) -> float:
    return 0.01 if symbol.endswith("JPY") else 0.0001


def point_size(symbol: str) -> float:
    return 0.001 if symbol.endswith("JPY") else 0.00001


def pip_value_usd_per_lot(symbol: str, price: float) -> float:
    quote_value = CONTRACT_SIZE * pip_size(symbol)
    if symbol.endswith("USD"):
        return quote_value
    if symbol.startswith("USD") and price > 0:
        return quote_value / price
    raise ValueError(f"unsupported symbol for USD conversion: {symbol}")


def quote_to_usd(symbol: str, amount: float, price: float) -> float:
    if symbol.endswith("USD"):
        return amount
    return amount / price


@dataclass(frozen=True)
class CostModel:
    name: str = "base"
    multiplier: float = 1.0
    slippage_pips: float = 0.2
    commission_per_lot_usd: float = 7.0  # round trip
    use_bar_spread: bool = True
    spread_floor_pips: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_SPREAD_FLOOR_PIPS))

    def spread_pips(self, symbol: str, bar_spread_points: float) -> float:
        recorded = bar_spread_points * point_size(symbol) / pip_size(symbol) if self.use_bar_spread else 0.0
        return max(float(recorded), float(self.spread_floor_pips.get(symbol, 1.0))) * self.multiplier

    @property
    def slippage(self) -> float:
        return self.slippage_pips * self.multiplier

    @property
    def commission(self) -> float:
        return self.commission_per_lot_usd * self.multiplier


COST_SCENARIOS = {
    "base": CostModel("base", 1.0),
    "stress_1_5x": CostModel("stress_1_5x", 1.5),
    "stress_2x": CostModel("stress_2x", 2.0),
}


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------
Frames = dict[str, dict[str, pd.DataFrame]]  # symbol -> timeframe name -> bars


def normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    if "spread_points" not in df.columns:
        df["spread_points"] = 0
    if "volume" not in df.columns:
        df["volume"] = df.get("tick_volume", 0)
    df = df[BAR_COLUMNS].astype(float)
    df.index = pd.DatetimeIndex(df.index).tz_convert("UTC").as_unit("ns")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def load_csv_frames(data_dir: Path, symbols: Sequence[str] = SYMBOLS,
                    timeframes: Sequence[str] = ("M15", "H1", "H4", "D1")) -> Frames:
    """Load UTC CSVs produced by validation.convert_mt5_history (``SYMBOL_TF.csv``)."""
    frames: Frames = {}
    for symbol in symbols:
        frames[symbol] = {}
        for tf in timeframes:
            path = Path(data_dir) / f"{symbol}_{tf}.csv"
            if not path.exists():
                if tf == "D1":
                    continue  # optional: PortfolioManager falls back to static correlations
                raise FileNotFoundError(path)
            raw = pd.read_csv(path)
            raw["time"] = pd.to_datetime(raw["time"], utc=True)
            frames[symbol][tf] = normalise_frame(raw.set_index("time"))
    return frames


def load_bridge_bar_exports(directory: Path, symbols: Sequence[str]) -> Frames:
    """Load the EA's live ``bars_SYMBOL_TF.json`` exports (used for live/replay parity)."""
    import json

    frames: Frames = {}
    for symbol in symbols:
        frames[symbol] = {}
        for tf in TIMEFRAME_SECONDS:
            path = Path(directory) / f"bars_{symbol}_{tf}.json"
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw = pd.DataFrame(payload["bars"], columns=payload["columns"])
            raw["time"] = pd.to_datetime(raw["time_utc"], unit="s", utc=True)
            raw = raw.rename(columns={"tick_volume": "volume"}).set_index("time")
            frames[symbol][tf] = normalise_frame(raw)
    return frames


class PointInTimeFeed:
    """Serves only bars that have closed at or before the current decision time."""

    def __init__(self, frames: Frames):
        self.frames = frames
        self._close_ns = {
            (symbol, tf): (df.index.asi8 + TIMEFRAME_SECONDS[tf] * 1_000_000_000)
            for symbol, tfs in frames.items() for tf, df in tfs.items()
        }
        self.now_ns: int | None = None
        self.lookback_override: int | None = None  # bias checks only

    def set_time(self, now: pd.Timestamp | datetime) -> None:
        self.now_ns = pd.Timestamp(now).tz_convert("UTC").value

    def closed_count(self, symbol: str, tf: str) -> int:
        if self.now_ns is None:
            raise RuntimeError("feed time not set")
        closes = self._close_ns.get((symbol, tf))
        if closes is None:
            return 0
        return int(np.searchsorted(closes, self.now_ns, side="right"))

    def fetch_ohlc(self, symbol: str, timeframe: int, num_candles: int) -> pd.DataFrame:
        tf = TIMEFRAME_NAMES.get(int(timeframe))
        df = self.frames.get(symbol, {}).get(tf) if tf else None
        if df is None:
            return pd.DataFrame()
        end = self.closed_count(symbol, tf)
        n = self.lookback_override or int(num_candles)
        return df.iloc[max(0, end - n):end]


# ----------------------------------------------------------------------
# Stage 1: candidate generation
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    decision_time: pd.Timestamp
    symbol: str
    signal: dict[str, Any]
    regime: dict[str, Any]


def _bar_spread_price(frames: Frames, feed: PointInTimeFeed, symbol: str, cost: CostModel) -> float:
    m15 = frames[symbol]["M15"]
    i = feed.closed_count(symbol, "M15")
    points = float(m15["spread_points"].iat[i - 1]) if i > 0 else 0.0
    return cost.spread_pips(symbol, points) * pip_size(symbol)


def generate_candidates(
    symbol: str,
    frames: Frames,
    policy: Mapping[str, object],
    cost: CostModel,
    *,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    decision_times: Sequence[pd.Timestamp] | None = None,
    lookback_override: int | None = None,
) -> tuple[list[Candidate], Counter]:
    feed = PointInTimeFeed(frames)
    feed.lookback_override = lookback_override
    spread_fn: Callable[[str], float] = lambda s: _bar_spread_price(frames, feed, s, cost)
    regime_agent = RegimeAgent(symbol, feed.fetch_ohlc, policy=policy)
    technical_agent = TechnicalAgent(symbol, feed.fetch_ohlc, spread_fn, policy=policy)
    m15 = frames[symbol]["M15"]
    if decision_times is None:
        times = m15.index + pd.Timedelta(seconds=TIMEFRAME_SECONDS["M15"])
        mask = np.ones(len(times), dtype=bool)
        if start is not None:
            mask &= times >= start
        if end is not None:
            mask &= times <= end
        times = times[mask]
    else:
        times = pd.DatetimeIndex(decision_times)

    funnel: Counter = Counter()
    candidates: list[Candidate] = []
    cached_h1 = -1
    regime: RegimeOutput | None = None
    for t in times:
        if not is_tradeable_session(t.to_pydatetime()):
            funnel["SESSION_INACTIVE"] += 1
            continue
        feed.set_time(t)
        h1_count = feed.closed_count(symbol, "H1")
        if h1_count != cached_h1 or regime is None:
            regime = regime_agent.evaluate(TIMEFRAME_CODES["H1"])
            cached_h1 = h1_count
        if regime.regime not in {"TRENDING_BULL", "TRENDING_BEAR"}:
            funnel[f"REGIME:{regime.reason_code}"] += 1
            continue
        signal = technical_agent.evaluate(regime, TIMEFRAME_CODES["M15"], TIMEFRAME_CODES["H1"])
        if signal is None:
            funnel[f"TECHNICAL:{technical_agent.last_reason_code}"] += 1
            continue
        funnel["CANDIDATE"] += 1
        candidates.append(Candidate(t, symbol, asdict(signal), asdict(regime)))
    return candidates, funnel


def _candidate_worker(args: tuple) -> tuple[list[Candidate], Counter]:
    symbol, data_dir, policy, cost, start, end = args
    frames = load_csv_frames(Path(data_dir), symbols=(symbol,))
    return generate_candidates(symbol, frames, policy, cost, start=start, end=end)


def generate_all_candidates(data_dir: Path, policy: Mapping[str, object], cost: CostModel, *,
                            symbols: Sequence[str] = SYMBOLS, start=None, end=None,
                            workers: int = 6) -> tuple[list[Candidate], dict[str, Counter]]:
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing as mp

    jobs = [(s, str(data_dir), dict(policy), cost, start, end) for s in symbols]
    if workers <= 1:
        results = [_candidate_worker(job) for job in jobs]
    else:
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs)), mp_context=mp.get_context("spawn")) as pool:
            results = list(pool.map(_candidate_worker, jobs))
    candidates = sorted((c for cands, _ in results for c in cands),
                        key=lambda c: (c.decision_time, SYMBOLS.index(c.symbol)))
    funnels = {s: f for s, (_, f) in zip(symbols, results)}
    return candidates, funnels


# ----------------------------------------------------------------------
# Stage 2: portfolio simulation
# ----------------------------------------------------------------------
EXIT_MODES = ("policy", "fixed_tp", "hybrid")


@dataclass
class Position:
    trade_id: str
    symbol: str
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    stop_pips: float
    stop_price: float
    target_price: float | None
    lots: float
    risk_percent: float
    risk_usd: float
    be_trigger_r: float
    partial_close_r: float
    trailing_atr_mult: float
    trail: bool
    regime: str
    volatility_state: str
    open_lots: float = 0.0
    partial_done: bool = False
    be_done: bool = False
    realised_usd: float = 0.0
    commission_usd: float = 0.0
    max_favourable_r: float = 0.0
    events: list[str] = field(default_factory=list)

    @property
    def stop_distance(self) -> float:
        return self.stop_pips * pip_size(self.symbol)


@dataclass(frozen=True)
class SimulationConfig:
    starting_balance: float = 100_000.0
    exit_mode: str = "policy"
    streak_reset: str = "week"  # "week": operator review clears a loss-streak halt each new ISO week
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None


@dataclass
class SimulationResult:
    trades: list[dict[str, Any]]
    equity: pd.DataFrame
    decisions: Counter
    risk_events: list[dict[str, Any]]
    config: dict[str, Any]


class _SymbolBars:
    def __init__(self, df: pd.DataFrame):
        self.times = df.index.asi8
        self.open = df["open"].to_numpy()
        self.high = df["high"].to_numpy()
        self.low = df["low"].to_numpy()
        self.close = df["close"].to_numpy()
        self.spread = df["spread_points"].to_numpy()
        self.atr = calculate_atr(df, EA_TRAIL_ATR_PERIOD).to_numpy()
        self.pos = {int(t): i for i, t in enumerate(self.times)}


def _apply_exit_mode(signal: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode == "policy":
        return signal
    if mode == "fixed_tp":
        return dict(signal, tp_mode="FIXED", be_trigger_r=0.0, partial_close_r=0.0, trailing_atr_mult=0.0)
    if mode == "hybrid":
        return dict(signal, tp_mode="HYBRID")
    raise ValueError(f"unknown exit mode {mode!r}; expected one of {EXIT_MODES}")


def ea_lot_size(symbol: str, balance: float, risk_percent: float, stop_pips: float, price: float) -> float:
    """FX_Execution CalculateLot: stop-distance sizing, floored to the lot step."""
    raw = balance * risk_percent / (stop_pips * pip_value_usd_per_lot(symbol, price))
    if raw < MIN_LOT:
        return 0.0
    return round(min(math.floor(raw / LOT_STEP + 1e-9) * LOT_STEP, MAX_LOT), 2)


def simulate(candidates: Sequence[Candidate], frames: Frames, policy: Mapping[str, object],
             cost: CostModel, config: SimulationConfig = SimulationConfig()) -> SimulationResult:
    feed = PointInTimeFeed(frames)
    spread_fn = lambda s: _bar_spread_price(frames, feed, s, cost)
    adversarial = {s: AdversarialAgent(s, feed.fetch_ohlc, spread_fn, policy=dict(policy)) for s in frames}
    portfolio = PortfolioManager(fetch_ohlc=feed.fetch_ohlc, policy=dict(policy))
    hard_risk = HardRiskEngine(policy=dict(policy))
    bars = {s: _SymbolBars(tfs["M15"]) for s, tfs in frames.items()}

    by_time: dict[int, list[Candidate]] = {}
    for c in candidates:
        if config.start is not None and c.decision_time < config.start:
            continue
        if config.end is not None and c.decision_time > config.end:
            continue
        by_time.setdefault(pd.Timestamp(c.decision_time).value, []).append(c)
    for items in by_time.values():
        items.sort(key=lambda c: SYMBOLS.index(c.symbol))

    timeline = np.unique(np.concatenate([b.times for b in bars.values()]))
    if config.start is not None:
        timeline = timeline[timeline >= pd.Timestamp(config.start).value - 900 * 10**9]
    if config.end is not None:
        timeline = timeline[timeline <= pd.Timestamp(config.end).value]

    account = AccountStatus(balance=config.starting_balance, equity=config.starting_balance,
                            peak_equity=config.starting_balance)
    positions: dict[str, Position] = {}
    pending: dict[str, tuple[Candidate, TechnicalSignal, float]] = {}
    trades: list[dict[str, Any]] = []
    risk_events: list[dict[str, Any]] = []
    decisions: Counter = Counter()
    equity_t, equity_v, balance_v = [], [], []
    day_key = week_key = None
    seq = 0

    def refresh_exposure() -> None:
        account.open_positions_count = len(positions) + len(pending)
        symbols_open = list(positions) + list(pending)
        account.open_symbols = symbols_open
        account.open_usd_exposure_count = sum(1 for s in set(symbols_open) if "USD" in s)
        account.open_risk_percent = round(sum(p.risk_percent for p in positions.values())
                                          + sum(r for _, _, r in pending.values()), 8)

    def close_position(pos: Position, when: pd.Timestamp, price: float, lots: float, reason: str) -> None:
        sign = 1.0 if pos.direction == "BUY" else -1.0
        pnl_quote = (price - pos.entry_price) * sign * lots * CONTRACT_SIZE
        pos.realised_usd += quote_to_usd(pos.symbol, pnl_quote, price)
        pos.commission_usd += cost.commission * lots
        pos.open_lots = round(pos.open_lots - lots, 2)
        pos.events.append(f"{reason}@{price:.5f}x{lots}")
        if pos.open_lots > 1e-9:
            return
        net = pos.realised_usd - pos.commission_usd
        account.balance += net
        del positions[pos.symbol]
        if net < 0:
            account.consecutive_losses += 1
        elif net > 0:
            account.consecutive_losses = 0
        trades.append({
            "trade_id": pos.trade_id, "symbol": pos.symbol, "direction": pos.direction,
            "entry_time": pos.entry_time.isoformat(), "exit_time": when.isoformat(), "exit_reason": reason,
            "entry_price": pos.entry_price, "exit_price": price, "stop_pips": pos.stop_pips,
            "lots": pos.lots, "risk_percent": pos.risk_percent, "risk_usd": pos.risk_usd,
            "gross_usd": pos.realised_usd, "commission_usd": pos.commission_usd, "net_usd": net,
            "r_multiple": net / pos.risk_usd if pos.risk_usd > 0 else 0.0,
            "max_favourable_r": pos.max_favourable_r, "partial": pos.partial_done, "break_even": pos.be_done,
            "regime": pos.regime, "volatility_state": pos.volatility_state,
            "hour_utc": pos.entry_time.hour, "events": ";".join(pos.events),
        })
        refresh_exposure()

    def open_position(symbol: str, cand: Candidate, signal: TechnicalSignal, risk_pct: float, i: int, t_ns: int) -> None:
        nonlocal seq
        b = bars[symbol]
        pip = pip_size(symbol)
        spread = cost.spread_pips(symbol, b.spread[i]) * pip
        if signal.direction == "BUY":
            entry = b.open[i] + spread + cost.slippage * pip
            stop = entry - signal.stop_pips * pip
            target = entry + signal.take_profit_pips * pip
        else:
            entry = b.open[i] - cost.slippage * pip
            stop = entry + signal.stop_pips * pip
            target = entry - signal.take_profit_pips * pip
        lots = ea_lot_size(symbol, account.balance, risk_pct, signal.stop_pips, entry)
        if lots <= 0:
            decisions["EA:REJECTED_LOT"] += 1
            return
        seq += 1
        trail = signal.tp_mode in {"TRAIL", "HYBRID"}
        hard_tp = signal.tp_mode in {"FIXED", "HYBRID"}
        positions[symbol] = Position(
            trade_id=f"RPL_{seq:06d}_{symbol}", symbol=symbol, direction=signal.direction,
            entry_time=pd.Timestamp(t_ns, tz="UTC"), entry_price=entry, stop_pips=signal.stop_pips,
            stop_price=stop, target_price=target if hard_tp else None, lots=lots, open_lots=lots,
            risk_percent=risk_pct, risk_usd=lots * signal.stop_pips * pip_value_usd_per_lot(symbol, entry),
            be_trigger_r=signal.be_trigger_r, partial_close_r=signal.partial_close_r,
            trailing_atr_mult=signal.trailing_atr_mult, trail=trail,
            regime=cand.regime["regime"], volatility_state=cand.regime["volatility_state"],
        )
        decisions["EXECUTED"] += 1

    def manage_bar(pos: Position, i: int, t_ns: int) -> None:
        b = bars[pos.symbol]
        pip = pip_size(pos.symbol)
        spread = cost.spread_pips(pos.symbol, b.spread[i]) * pip
        start = pd.Timestamp(t_ns, tz="UTC")
        quote = QuoteBar(start, start + pd.Timedelta(minutes=15),
                         b.open[i], b.high[i], b.low[i], b.close[i],
                         b.open[i] + spread, b.high[i] + spread, b.low[i] + spread, b.close[i] + spread)
        far = pos.entry_price * (10 if pos.direction == "BUY" else 0.1)
        target = pos.target_price if pos.target_price is not None else far
        fill = protective_exit(direction=pos.direction, active_before=start - pd.Timedelta(microseconds=1),
                               quote=quote, stop_price=pos.stop_price, target_price=target,
                               pip_size=pip, slippage_pips=cost.slippage)
        if fill is not None:
            close_position(pos, fill.timestamp, fill.price, pos.open_lots, fill.reason + ("_AMBIGUOUS" if fill.ambiguous else ""))
            return
        # Favourable extreme of the bar on the exit side of the book.
        if pos.direction == "BUY":
            extreme = quote.bid_high
            fav_r = (extreme - pos.entry_price) / pos.stop_distance
        else:
            extreme = quote.ask_low
            fav_r = (pos.entry_price - extreme) / pos.stop_distance
        pos.max_favourable_r = max(pos.max_favourable_r, fav_r)
        sign = 1.0 if pos.direction == "BUY" else -1.0
        if pos.partial_close_r > 0 and not pos.partial_done and fav_r >= pos.partial_close_r:
            half = math.floor(pos.open_lots * 0.5 / LOT_STEP + 1e-9) * LOT_STEP
            if half >= MIN_LOT and pos.open_lots > MIN_LOT:
                level = pos.entry_price + sign * pos.partial_close_r * pos.stop_distance - sign * cost.slippage * pip
                pos.partial_done = True
                close_position(pos, quote.end, level, round(half, 2), "PARTIAL")
        if pos.be_trigger_r > 0 and fav_r >= pos.be_trigger_r:
            be_price = pos.entry_price + sign * EA_BREAK_EVEN_BUFFER_PIPS * pip
            if (be_price - pos.stop_price) * sign > 0:
                pos.stop_price = be_price
                pos.be_done = True
        if pos.trail and pos.trailing_atr_mult > 0 and fav_r >= pos.be_trigger_r and not math.isnan(b.atr[i]):
            trail_price = extreme - sign * b.atr[i] * pos.trailing_atr_mult
            if (trail_price - pos.stop_price) * sign > 0 and (extreme - trail_price) * sign > 0:
                pos.stop_price = trail_price

    def mark_to_market(t_ns: int) -> None:
        floating = 0.0
        for pos in positions.values():
            b = bars[pos.symbol]
            j = int(np.searchsorted(b.times, t_ns, side="right")) - 1
            if j < 0:
                continue
            pip = pip_size(pos.symbol)
            price = b.close[j] if pos.direction == "BUY" else b.close[j] + cost.spread_pips(pos.symbol, b.spread[j]) * pip
            sign = 1.0 if pos.direction == "BUY" else -1.0
            floating += quote_to_usd(pos.symbol, (price - pos.entry_price) * sign * pos.open_lots * CONTRACT_SIZE, price)
        account.floating_pnl = floating
        account.equity = account.balance + floating
        account.peak_equity = max(account.peak_equity, account.equity)
        account.drawdown_percent = max(0.0, (account.peak_equity - account.equity) / account.peak_equity)
        account.daily_loss_percent = max(0.0, (account.daily_anchor_equity - account.equity) / account.daily_anchor_equity)
        account.weekly_loss_percent = max(0.0, (account.weekly_anchor_equity - account.equity) / account.weekly_anchor_equity)

    for t_ns in timeline:
        t_ns = int(t_ns)
        bar_start = pd.Timestamp(t_ns, tz="UTC")
        # Anchors roll at the first bar of a new UTC day / ISO week.
        d_key = bar_start.strftime("%Y-%m-%d")
        iso = bar_start.isocalendar()
        w_key = f"{iso[0]}-W{iso[1]:02d}"
        if d_key != day_key:
            day_key = d_key
            account.daily_anchor_date = d_key
            account.daily_anchor_equity = account.equity
        if w_key != week_key:
            week_key = w_key
            account.weekly_anchor_key = w_key
            account.weekly_anchor_equity = account.equity
            if config.streak_reset == "week" and account.consecutive_losses >= hard_risk.LOSS_HALT_THRESHOLD:
                risk_events.append({"time": bar_start.isoformat(), "rule": "LOSS_STREAK_OPERATOR_RESET",
                                    "consecutive_losses": account.consecutive_losses})
                account.consecutive_losses = 0

        # Entries approved at the previous decision fill at this bar's open.
        for symbol in list(pending):
            i = bars[symbol].pos.get(t_ns)
            if i is None:
                continue
            cand, signal, risk_pct = pending.pop(symbol)
            open_position(symbol, cand, signal, risk_pct, i, t_ns)
            refresh_exposure()
        for symbol in list(positions):
            i = bars[symbol].pos.get(t_ns)
            if i is not None:
                manage_bar(positions[symbol], i, t_ns)

        decision_ns = t_ns + 900 * 10**9
        mark_to_market(decision_ns)
        equity_t.append(decision_ns)
        equity_v.append(account.equity)
        balance_v.append(account.balance)

        for cand in by_time.get(decision_ns, []):
            symbol = cand.symbol
            if symbol in pending:
                decisions["SIM:ENTRY_ALREADY_PENDING"] += 1
                continue
            feed.set_time(cand.decision_time)
            signal = TechnicalSignal(**_apply_exit_mode(cand.signal, config.exit_mode))
            regime = RegimeOutput(**cand.regime)
            adv = adversarial[symbol].evaluate(signal, account, TIMEFRAME_CODES["M15"], now_utc=cand.decision_time.to_pydatetime())
            if not adv.approved:
                decisions[f"ADVERSARIAL:{adv.reason_code}"] += 1
                continue
            pm = portfolio.evaluate(signal, adv, account, open_symbols=account.open_symbols, regime=regime)
            if not pm.approved:
                decisions[f"PORTFOLIO:{pm.reason_code}"] += 1
                continue
            risk = hard_risk.validate(account, pm.final_risk_percent)
            if not risk.approved:
                decisions[f"HARD_RISK:{risk.reason_code}"] += 1
                if not risk_events or risk_events[-1].get("rule") != risk.reason_code:
                    risk_events.append({"time": cand.decision_time.isoformat(), "rule": risk.reason_code})
                continue
            final_risk = round(pm.final_risk_percent * risk.risk_throttle_multiplier, 8)
            pending[symbol] = (cand, signal, final_risk)
            decisions["APPROVED"] += 1
            refresh_exposure()

    if positions:
        last_ns = int(timeline[-1]) if len(timeline) else 0
        for pos in list(positions.values()):
            b = bars[pos.symbol]
            j = int(np.searchsorted(b.times, last_ns, side="right")) - 1
            pip = pip_size(pos.symbol)
            price = b.close[j] if pos.direction == "BUY" else b.close[j] + cost.spread_pips(pos.symbol, b.spread[j]) * pip
            close_position(pos, pd.Timestamp(b.times[j], tz="UTC") + pd.Timedelta(minutes=15), price, pos.open_lots, "END_OF_DATA")

    equity = pd.DataFrame({"equity": equity_v, "balance": balance_v},
                          index=pd.to_datetime(np.array(equity_t, dtype="int64"), utc=True))
    return SimulationResult(trades, equity, decisions, risk_events,
                            {"cost": asdict(cost), "simulation": {k: str(v) for k, v in asdict(config).items()}})
