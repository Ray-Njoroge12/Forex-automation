"""Deterministic synthetic demo-account fixture with planted effects (wave 3: R7, R8, R9).

Synthetic data, NOT EVIDENCE. The fixture imitates the records the live engine
and the EA leave behind (state DB, deals.json, exit_*.json, execution feedback,
M15 bar exports, economic calendar) so the wave 3 tools can be tested against
known answers. It writes only inside ``out_dir``; the DB is created from
database/schema.sql (read, never modified) and never from the live DB.

Planted effects (all listed in manifest.json):
  * 30 ordinary closed trades over ISO weeks 2026-W45 and 2026-W46: decision
    at bar close + 3 s, entry deal at bar close + 6 s, actual spread = replay
    base floor + 0.5 pip (+/- 0.1 pip jitter in whole points), entry slippage
    0.3 pip (model 0.2), commission equal to the model, stop exits 0.4 pip
    adverse, targets at the level, ADX at entry >= 25, no regime change and no
    high-impact news inside their lives, stops 15-25 pips;
  * SYN_EXEC (fill 120 s after bar close), SYN_NEWS (USD high-impact event
    10 min after entry), SYN_COST (3-pip stop), SYN_REGIME (REGIME_TRANSITION
    reject during the trade), SYN_MANUAL (exit deal magic 0, no sl/tp tag),
    SYN_MISSING (closed row with no deal and no feedback), SYN_OPEN (still
    open), and a demo-matrix deal SYN_MATRIX with no trades row.
The bar path is a seeded random walk; it is not conditioned on trade outcomes.

Usage (from fx_ai_engine/):
    USE_MT5_MOCK=1 ../.venv/bin/python -m research.synthetic_demo --out ../tmp/wave3-fixture --seed 3
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtesting.unified_replay import COST_SCENARIOS, DEFAULT_SPREAD_FLOOR_PIPS, pip_size, pip_value_usd_per_lot, point_size
from config_microcapital import get_policy_config
from research.common import PROJECT_ROOT
from research.demo_records import DEFAULT_STREAM, EA_MAGIC, guard_output_dir

SCHEMA_PATH = PROJECT_ROOT / "database" / "schema.sql"
FIXTURE_SYMBOLS = ("EURUSD", "GBPUSD")
START_PRICE_POINTS = {"EURUSD": 116_000, "GBPUSD": 134_000}
BARS_START = pd.Timestamp("2026-11-02T00:00:00Z")
BARS_END = pd.Timestamp("2026-11-13T21:00:00Z")
SLOT_HOURS = (7, 10, 13, 16)
SERVER_OFFSET_S = 7200
BAR_SPREAD_POINTS = 3
BALANCE_USD = 10_000.0
REPLAY_ROWS = 200
REPLAY_START = pd.Timestamp("2025-01-06T00:00:00Z")
WIN_PROB, WIN_R, LOSS_R = 0.35, 2.2, -1.0

SIGNAL_DELAY_S = 3
ENTRY_DELAY_S = 6
EXEC_DELAY_S = 120
SPREAD_EXCESS_PIPS = 0.5
SPREAD_JITTER_POINTS = (-1, 0, 1)
ENTRY_SLIPPAGE_PIPS = 0.3
STOP_EXIT_SLIPPAGE_PIPS = 0.4
ORDINARY_STOP_PIPS = (15, 25)
ORDINARY_ADX = (25.0, 35.0)
COST_STOP_PIPS = 3
NEWS_AFTER_ENTRY_MIN = 10
MANUAL_EXIT_R = 0.5

# slot index -> special trade; every other slot below 36 is an ordinary trade
SPECIAL_SLOTS = {5: "SYN_EXEC", 10: "SYN_NEWS", 17: "SYN_COST", 22: "SYN_REGIME",
                 29: "SYN_MANUAL", 32: "SYN_MISSING", 36: "SYN_OPEN"}
MATRIX_SLOT = 37
SPECIAL_EFFECTS = {
    "SYN_EXEC": ("entry deal 120 s after bar close (price ordinary); loss", "EXECUTION"),
    "SYN_NEWS": ("USD high-impact event 10 min after entry; loss", "NEWS_CALENDAR"),
    "SYN_COST": ("3-pip stop so spread+slippage+commission >= 0.25 R; loss", "SPREAD_COST"),
    "SYN_REGIME": ("REGIME REJECT REGIME_TRANSITION row for its symbol during the trade; loss", "REGIME_MISMATCH"),
    "SYN_MANUAL": ("exit deal magic 0 with empty comment (not closed by the EA); win", "EXECUTION"),
    "SYN_MISSING": ("CLOSED_LOSS row with no deal and no execution feedback", "INSUFFICIENT_DATA note"),
    "SYN_OPEN": ("EXECUTED_OPEN with an entry deal only", "not closed"),
    "SYN_MATRIX": ("demo-matrix deal pair with feedback file only, no trades row", "not a strategy trade"),
}


def _iso(ts: pd.Timestamp) -> str:
    return ts.isoformat()


def _iso_z(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _msc(ts: pd.Timestamp) -> int:
    return int(ts.value // 1_000_000)


def _dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=False) + "\n", encoding="utf-8")


def _bar_times() -> list[pd.Timestamp]:
    times = pd.date_range(BARS_START, BARS_END, freq="15min", tz="UTC")
    return [t for t in times if t.weekday() < 5]


def _slots() -> list[pd.Timestamp]:
    days = sorted({t.normalize() for t in _bar_times()})
    return [d + pd.Timedelta(hours=h) for d in days for h in SLOT_HOURS]


def _build_bars(rng: np.random.Generator) -> dict[str, dict[pd.Timestamp, list[int]]]:
    times = _bar_times()
    bars: dict[str, dict[pd.Timestamp, list[int]]] = {}
    for symbol in FIXTURE_SYMBOLS:
        price = START_PRICE_POINTS[symbol]
        rows: dict[pd.Timestamp, list[int]] = {}
        for t in times:
            o = price
            c = o + int(round(rng.normal(0.0, 30.0)))
            h = max(o, c) + int(abs(round(rng.normal(0.0, 15.0))))
            low = min(o, c) - int(abs(round(rng.normal(0.0, 15.0))))
            rows[t] = [o, h, low, c, int(rng.integers(100, 1000))]
            price = c
        bars[symbol] = rows
    return bars


def _bars_payload(symbol: str, rows: dict[pd.Timestamp, list[int]]) -> dict:
    pt = point_size(symbol)
    data = [[int(t.timestamp()), round(o * pt, 5), round(h * pt, 5), round(lo * pt, 5), round(c * pt, 5), tv,
             BAR_SPREAD_POINTS] for t, (o, h, lo, c, tv) in rows.items()]
    return {"schema_version": 1, "symbol": symbol, "timeframe": "M15", "generated_at": _iso_z(BARS_END),
            "server_utc_offset_seconds": SERVER_OFFSET_S,
            "columns": ["time_utc", "open", "high", "low", "close", "tick_volume", "spread_points"], "bars": data}


def _deal(ticket: int, order: int, position: int, time: pd.Timestamp, side: int, entry: int, symbol: str,
          volume: float, price: float, profit: float, commission: float, magic: int, comment: str) -> dict:
    return {"ticket": ticket, "order": order, "position_id": position, "time_msc": _msc(time), "type": side,
            "entry": entry, "symbol": symbol, "volume": volume, "price": price, "profit": profit,
            "commission": commission, "swap": 0.0, "fee": 0.0, "magic": magic, "comment": comment}


def _plan_trades(rng: np.random.Generator, bars: dict[str, dict[pd.Timestamp, list[int]]]) -> list[dict]:
    policy = get_policy_config(mode_id="core_srs", env={})
    risk_pct = float(policy["BASE_RISK_PCT"])
    rr = float(policy["MIN_RISK_REWARD"])
    risk_usd = round(BALANCE_USD * risk_pct, 2)
    slots = _slots()
    trades: list[dict] = []
    for idx in range(MATRIX_SLOT):
        # Draw every random quantity for every slot so the stream never depends on the special layout.
        direction = "BUY" if rng.random() < 0.5 else "SELL"
        stop = int(rng.integers(ORDINARY_STOP_PIPS[0], ORDINARY_STOP_PIPS[1] + 1))
        adx = round(float(rng.uniform(*ORDINARY_ADX)), 2)
        jitter = int(SPREAD_JITTER_POINTS[int(rng.integers(0, len(SPREAD_JITTER_POINTS)))])
        duration_bars = int(rng.integers(2, 10))
        exit_extra_ms = int(rng.integers(5_000, 600_000))
        drawn_r = WIN_R if rng.random() < WIN_PROB else LOSS_R
        kind = SPECIAL_SLOTS.get(idx, "ORDINARY")
        trade_id = kind if kind != "ORDINARY" else f"AI_SYN_{idx:03d}"
        symbol = FIXTURE_SYMBOLS[idx % 2]
        bar_close = slots[idx]
        if kind == "SYN_COST":
            stop = COST_STOP_PIPS
        r = drawn_r if kind == "ORDINARY" else (MANUAL_EXIT_R if kind == "SYN_MANUAL" else LOSS_R)
        if kind == "SYN_OPEN":
            r = 0.0
        exit_kind = "MANUAL" if kind == "SYN_MANUAL" else ("TP" if r > 0 else "SL")

        pt_per_pip = int(round(pip_size(symbol) / point_size(symbol)))
        bid_open = bars[symbol][bar_close][0]
        spread_pts = int(round(DEFAULT_SPREAD_FLOOR_PIPS[symbol] * pt_per_pip)) + int(round(SPREAD_EXCESS_PIPS * pt_per_pip)) + jitter
        slip_pts = int(round(ENTRY_SLIPPAGE_PIPS * pt_per_pip))
        s = 1 if direction == "BUY" else -1
        fill_pts = bid_open + spread_pts + slip_pts if s == 1 else bid_open - slip_pts
        tp_pips = round(stop * rr, 2)
        sl_pts = fill_pts - s * stop * pt_per_pip
        tp_pts = fill_pts + s * int(round(tp_pips * pt_per_pip))
        if exit_kind == "TP":
            exit_pts, comment = tp_pts, f"[tp {tp_pts * point_size(symbol):.5f}]"
        elif exit_kind == "SL":
            exit_pts = sl_pts - s * int(round(STOP_EXIT_SLIPPAGE_PIPS * pt_per_pip))
            comment = f"[sl {sl_pts * point_size(symbol):.5f}]"
        else:
            exit_pts, comment = fill_pts + s * int(round(stop * MANUAL_EXIT_R * pt_per_pip)), ""
        price = fill_pts * point_size(symbol)
        lots = round(risk_usd / (stop * pip_value_usd_per_lot(symbol, price)), 2)

        decision_time = bar_close + pd.Timedelta(seconds=SIGNAL_DELAY_S)
        entry_time = bar_close + pd.Timedelta(seconds=EXEC_DELAY_S if kind == "SYN_EXEC" else ENTRY_DELAY_S)
        exit_time = bar_close + pd.Timedelta(minutes=15 * duration_bars) + pd.Timedelta(milliseconds=exit_extra_ms)
        trades.append({
            "slot": idx, "kind": kind, "trade_id": trade_id, "symbol": symbol, "direction": direction,
            "bar_close": bar_close, "decision_time": decision_time, "entry_time": entry_time,
            "exit_time": None if kind == "SYN_OPEN" else exit_time, "duration_bars": duration_bars,
            "stop_pips": float(stop), "tp_pips": tp_pips, "adx": adx, "rr": rr, "risk_pct": risk_pct,
            "risk_usd": risk_usd, "lots": lots, "r_multiple": r, "profit_loss": round(r * risk_usd, 2),
            "bid_open_pts": bid_open, "spread_pts": spread_pts, "slip_pts": slip_pts, "fill_pts": fill_pts,
            "exit_pts": exit_pts, "exit_kind": exit_kind, "exit_comment": comment,
            "position_ticket": 59_200_000_000 + idx, "entry_ticket": 58_800_000_000 + 2 * idx,
            "exit_ticket": 58_800_000_000 + 2 * idx + 1,
        })
    return trades


def _matrix(bars: dict[str, dict[pd.Timestamp, list[int]]]) -> dict:
    symbol = FIXTURE_SYMBOLS[MATRIX_SLOT % 2]
    bar_close = _slots()[MATRIX_SLOT]
    pt_per_pip = int(round(pip_size(symbol) / point_size(symbol)))
    bid_open = bars[symbol][bar_close][0]
    fill_pts = bid_open + int(round(DEFAULT_SPREAD_FLOOR_PIPS[symbol] * pt_per_pip))
    sl_pts = fill_pts - 3 * pt_per_pip
    return {"trade_id": "SYN_MATRIX", "symbol": symbol, "bar_close": bar_close,
            "entry_time": bar_close + pd.Timedelta(seconds=ENTRY_DELAY_S),
            "exit_time": bar_close + pd.Timedelta(minutes=12, milliseconds=250),
            "fill_pts": fill_pts, "sl_pts": sl_pts, "lots": 1.0, "position_ticket": 59_200_000_000 + MATRIX_SLOT,
            "entry_ticket": 58_800_000_000 + 2 * MATRIX_SLOT, "exit_ticket": 58_800_000_000 + 2 * MATRIX_SLOT + 1}


def _funnel_rows(t: dict) -> list[tuple]:
    sym, d, dt = t["symbol"], t["direction"], _iso(t["decision_time"])
    bull = d == "BUY"
    close = t["bid_open_pts"] * point_size(sym)
    session = "london" if t["bar_close"].hour < 12 else "newyork"
    regime = (f"regime={'TRENDING_BULL' if bull else 'TRENDING_BEAR'} trend_state={'BULLISH' if bull else 'BEARISH'} "
              f"volatility=NORMAL direction_candidate={d} close={close:.5f} adx={t['adx']:.2f} "
              f"adx_no_trade_below=15.00 adx_transition_below=20.00 atr_ratio=1.0000")
    technical = (f"direction={d} rr={t['rr']:.2f} spread_pips={BAR_SPREAD_POINTS * point_size(sym) / pip_size(sym):.2f} "
                 f"stop_pips={t['stop_pips']:.2f} tp_pips={t['tp_pips']:.2f}")
    tid = t["trade_id"]
    rows = [
        (dt, sym, "SESSION", "PASS", "SESSION_ACTIVE", f"session={session}", None),
        (dt, sym, "REGIME", "PASS", "REGIME_TREND_BULL" if bull else "REGIME_TREND_BEAR", regime, None),
        (dt, sym, "TECHNICAL", "PASS", f"TECH_CONFIRMED_{d}", technical, tid),
        (dt, sym, "ADVERSARIAL", "PASS", "ADV_APPROVED", "No adversarial blocker triggered", tid),
        (dt, sym, "PORTFOLIO", "PASS", "PM_APPROVED", f"Portfolio constraints satisfied [mode=core_srs pct_base={t['risk_pct']:.4f}]", tid),
        (dt, sym, "HARD_RISK", "PASS", "RISK_APPROVED", "Hard risk constraints satisfied [mode=core_srs]", tid),
        (dt, sym, "ROUTER", "ROUTED", "ROUTED_TO_MT5", f"risk_percent={t['risk_pct']:.8f}", tid),
    ]
    if t["kind"] == "SYN_REGIME":
        mid = _iso(t["bar_close"] + pd.Timedelta(minutes=15 * (t["duration_bars"] // 2), seconds=SIGNAL_DELAY_S))
        rows += [
            (mid, sym, "SESSION", "PASS", "SESSION_ACTIVE", f"session={session}", None),
            (mid, sym, "REGIME", "REJECT", "REGIME_TRANSITION",
             f"regime=TRANSITION trend_state=FLAT volatility=NORMAL direction_candidate={d} close={close:.5f} "
             f"adx=18.40 adx_no_trade_below=15.00 adx_transition_below=20.00 atr_ratio=1.0000", None),
            (mid, sym, "TECHNICAL", "SKIP", "TECH_SKIPPED_REGIME_REJECTED",
             "regime=TRANSITION regime_reason=REGIME_TRANSITION trend_state=FLAT", None),
        ]
    return rows


def _write_db(path: Path, trades: list[dict]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for t in trades:
            sym, pt = t["symbol"], point_size(t["symbol"])
            has_deal = t["kind"] != "SYN_MISSING"
            if t["kind"] == "SYN_OPEN":
                status, reason = "EXECUTED_OPEN", "EXECUTED"
            else:
                status, reason = ("CLOSED_WIN" if t["r_multiple"] > 0 else "CLOSED_LOSS"), "EXIT_FEEDBACK"
            spread_price = round(t["spread_pts"] * pt, 6)
            slip_price = round(t["slip_pts"] * pt, 6)
            conn.execute(
                "INSERT INTO trades (trade_id, trade_ticket, position_ticket, evidence_stream, policy_mode, execution_mode,"
                " account_scope, symbol, direction, order_type, lot_size, entry_price, stop_loss, take_profit, risk_percent,"
                " market_regime, status, reason_code, profit_loss, r_multiple, spread_entry, spread_signal_pips,"
                " spread_exec_price, slippage, execution_time, open_time, close_time, risk_reward)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["trade_id"], t["entry_ticket"] if has_deal else None, t["position_ticket"] if has_deal else None,
                 DEFAULT_STREAM, "core_srs", "mt5", "synthetic:fixture", sym, t["direction"], "MARKET", t["lots"],
                 round(t["fill_pts"] * pt, 5), t["stop_pips"], t["tp_pips"], t["risk_pct"],
                 "TRENDING_BULL" if t["direction"] == "BUY" else "TRENDING_BEAR", status, reason, t["profit_loss"],
                 t["r_multiple"], 0.0, BAR_SPREAD_POINTS * pt / pip_size(sym), spread_price if has_deal else None,
                 slip_price if has_deal else None, _iso_z(t["entry_time"]) if has_deal else None,
                 _iso(t["decision_time"]), _iso(t["exit_time"]) if t["exit_time"] is not None else None, t["rr"]))
            for row in _funnel_rows(t):
                conn.execute(
                    "INSERT INTO decision_funnel_events (timestamp, evidence_stream, policy_mode, execution_mode,"
                    " account_scope, decision_time, symbol, stage, outcome, reason_code, details, trade_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row[0], DEFAULT_STREAM, "core_srs", "mt5", "synthetic:fixture", *row))
            if has_deal:
                payload = {"broker_deal": t["entry_ticket"], "broker_order": t["position_ticket"],
                           "close_time": _iso_z(t["entry_time"]), "entry_price": round(t["fill_pts"] * pt, 5),
                           "lot_size": t["lots"], "position_ticket": t["position_ticket"], "profit_loss": 0.0,
                           "r_multiple": 0.0, "server_retcode": 10009, "slippage": slip_price,
                           "spread_at_entry": spread_price, "status": "EXECUTED", "ticket": t["entry_ticket"],
                           "trade_id": t["trade_id"]}
                conn.execute(
                    "INSERT INTO trade_lifecycle_events (trade_id, event_type, status, reason_code, payload_json, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (t["trade_id"], "EXECUTION", "EXECUTED_OPEN", "EXECUTED", json.dumps(payload, sort_keys=True),
                     _iso(t["entry_time"] + pd.Timedelta(seconds=4))))
        conn.commit()
    finally:
        conn.close()


def build_fixture(out_dir: str | Path, seed: int = 3, *, force: bool = False) -> dict:
    """Write the synthetic fixture into ``out_dir`` and return the manifest."""
    out = guard_output_dir(out_dir)
    if out == (PROJECT_ROOT / "database").resolve():
        raise ValueError("refusing to build the fixture inside fx_ai_engine/database")
    db_path = out / "trading_state.db"
    if db_path.exists():
        if not force:
            raise FileExistsError(f"{db_path} exists; pass force=True to rebuild")
        db_path.unlink()
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    cost = COST_SCENARIOS["base"]
    commission_per_deal = cost.commission_per_lot_usd / 2.0

    bars = _build_bars(rng)
    for symbol in FIXTURE_SYMBOLS:
        _dump(out / "bars" / f"bars_{symbol}_M15.json", _bars_payload(symbol, bars[symbol]))

    replay_r = [WIN_R if x < WIN_PROB else LOSS_R for x in rng.random(REPLAY_ROWS)]
    replay_lines = ["trade_id,exit_time,r_multiple"] + [
        f"RPL_{i:03d},{_iso(REPLAY_START + pd.Timedelta(minutes=15 * i))},{r}" for i, r in enumerate(replay_r)]
    (out / "replay_trades.csv").write_text("\n".join(replay_lines) + "\n", encoding="utf-8")

    trades = _plan_trades(rng, bars)
    matrix = _matrix(bars)
    deals: list[dict] = []
    for t in trades:
        if t["kind"] == "SYN_MISSING":
            continue
        pt, s = point_size(t["symbol"]), 1 if t["direction"] == "BUY" else -1
        comm = -round(commission_per_deal * t["lots"], 2)
        deals.append(_deal(t["entry_ticket"], t["position_ticket"], t["position_ticket"], t["entry_time"],
                           0 if s == 1 else 1, 0, t["symbol"], t["lots"], round(t["fill_pts"] * pt, 5), 0.0, comm,
                           EA_MAGIC, t["trade_id"]))
        if t["exit_time"] is None:
            continue
        gross = round(t["profit_loss"] - 2 * comm, 2)
        deals.append(_deal(t["exit_ticket"], t["position_ticket"] + 1_000_000, t["position_ticket"], t["exit_time"],
                           1 if s == 1 else 0, 1, t["symbol"], t["lots"], round(t["exit_pts"] * pt, 5), gross, comm,
                           0 if t["kind"] == "SYN_MANUAL" else EA_MAGIC, t["exit_comment"]))
        _dump(out / "exits" / f"exit_{t['position_ticket']}.json", {
            "ticket": t["position_ticket"], "position_ticket": t["position_ticket"], "trade_id": t["trade_id"],
            "profit_loss": t["profit_loss"], "status": "CLOSED_WIN" if t["r_multiple"] > 0 else "CLOSED_LOSS",
            "is_final_exit": True, "r_multiple": t["r_multiple"],
            "close_time": _iso_z(t["exit_time"].floor("s") + pd.Timedelta(seconds=SERVER_OFFSET_S))})

    m, mpt = matrix, point_size(matrix["symbol"])
    m_comm = -round(commission_per_deal * m["lots"], 2)
    m_loss = round((m["sl_pts"] - m["fill_pts"]) * mpt * 100_000 * m["lots"], 2)
    deals.append(_deal(m["entry_ticket"], m["position_ticket"], m["position_ticket"], m["entry_time"], 0, 0,
                       m["symbol"], m["lots"], round(m["fill_pts"] * mpt, 5), 0.0, m_comm, EA_MAGIC, "SYN_MATRIX"))
    deals.append(_deal(m["exit_ticket"], m["position_ticket"] + 1_000_000, m["position_ticket"], m["exit_time"], 1, 1,
                       m["symbol"], m["lots"], round(m["sl_pts"] * mpt, 5), m_loss, m_comm, EA_MAGIC,
                       f"[sl {m['sl_pts'] * mpt:.5f}]"))
    _dump(out / "exits" / f"exit_{m['position_ticket']}.json", {
        "ticket": m["position_ticket"], "position_ticket": m["position_ticket"], "trade_id": "SYN_MATRIX",
        "profit_loss": round(m_loss + 2 * m_comm, 2), "status": "CLOSED_LOSS", "is_final_exit": True,
        "r_multiple": -1.0, "close_time": _iso_z(m["exit_time"].floor("s") + pd.Timedelta(seconds=SERVER_OFFSET_S))})
    m_spread = round(DEFAULT_SPREAD_FLOOR_PIPS[m["symbol"]] * pip_size(m["symbol"]), 6)
    _dump(out / "feedback" / "execution_SYN_MATRIX.json", {
        "trade_id": "SYN_MATRIX", "ticket": m["entry_ticket"], "position_ticket": m["position_ticket"],
        "status": "EXECUTED", "entry_price": round(m["fill_pts"] * mpt, 5), "slippage": 0.0,
        "spread_at_entry": m_spread, "lot_size": m["lots"], "profit_loss": 0.0, "r_multiple": 0.0,
        "server_retcode": 10009, "broker_order": m["position_ticket"], "broker_deal": m["entry_ticket"],
        "close_time": _iso_z(m["entry_time"])})
    _dump(out / "feedback" / "payload.json", {
        "trade_id": "SYN_MATRIX", "symbol": m["symbol"], "direction": "BUY", "risk_percent": 0.001,
        "stop_pips": 3.0, "take_profit_pips": 6.6, "timestamp_utc": _iso(m["bar_close"]), "order_type": "MARKET",
        "tp_mode": "FIXED", "schema_version": 2, "policy_mode": "core_srs"})

    deals.sort(key=lambda d: (d["time_msc"], d["ticket"]))
    _dump(out / "deals.json", {"schema_version": 1, "generated_at": _iso_z(BARS_END),
                               "server_utc_offset_seconds": SERVER_OFFSET_S, "lookback_days": 30, "deals": deals})

    news = next(t for t in trades if t["kind"] == "SYN_NEWS")
    news_time = news["bar_close"] + pd.Timedelta(minutes=NEWS_AFTER_ENTRY_MIN)
    _dump(out / "economic_calendar.json", [
        {"datetime_utc": _iso_z(BARS_START), "currency": "USD", "impact": "low",
         "event": "Synthetic coverage marker (start)", "source_url": "synthetic"},
        {"datetime_utc": _iso_z(news_time), "currency": "USD", "impact": "high",
         "event": "Synthetic high-impact release", "source_url": "synthetic"},
        {"datetime_utc": _iso_z(BARS_END), "currency": "USD", "impact": "low",
         "event": "Synthetic coverage marker (end)", "source_url": "synthetic"},
    ])

    _write_db(db_path, trades)

    ordinary = [t for t in trades if t["kind"] == "ORDINARY"]
    manifest = {
        "tool": "synthetic_demo",
        "note": "Synthetic fixture with planted effects; NOT EVIDENCE.",
        "seed": seed,
        "evidence_stream": DEFAULT_STREAM,
        "files": ["trading_state.db", "deals.json", "bars/bars_EURUSD_M15.json", "bars/bars_GBPUSD_M15.json",
                  "exits/", "feedback/execution_SYN_MATRIX.json", "feedback/payload.json",
                  "economic_calendar.json", "replay_trades.csv", "manifest.json"],
        "bars": {"symbols": list(FIXTURE_SYMBOLS), "start": _iso(BARS_START), "end": _iso(BARS_END),
                 "weekdays_only": True, "spread_points": BAR_SPREAD_POINTS,
                 "note": "seeded random walk; not conditioned on trade outcomes"},
        "replay_trades": {"rows": REPLAY_ROWS, "win_prob": WIN_PROB, "win_r": WIN_R, "loss_r": LOSS_R,
                          "start": _iso(REPLAY_START), "step_minutes": 15},
        "planted_effects": {
            "ordinary_trades": len(ordinary),
            "signal_latency_s": SIGNAL_DELAY_S,
            "entry_latency_s": ENTRY_DELAY_S,
            "spread_excess_over_model_pips": SPREAD_EXCESS_PIPS,
            "spread_jitter_pips": [j * 0.1 for j in SPREAD_JITTER_POINTS],
            "entry_slippage_pips": ENTRY_SLIPPAGE_PIPS,
            "model_slippage_pips": cost.slippage_pips,
            "stop_exit_adverse_slippage_pips": STOP_EXIT_SLIPPAGE_PIPS,
            "commission_per_lot_per_deal_usd": -commission_per_deal,
            "model_commission_round_trip_usd": cost.commission_per_lot_usd,
            "model_spread_floor_pips": {s: DEFAULT_SPREAD_FLOOR_PIPS[s] for s in FIXTURE_SYMBOLS},
            "ordinary_stop_pips": list(ORDINARY_STOP_PIPS),
            "ordinary_adx_at_entry": list(ORDINARY_ADX),
            "exit_file_close_time_offset_s": SERVER_OFFSET_S,
            "news_event_utc": _iso_z(news_time),
            "expected_r7": {"mean_spread_diff_pips": SPREAD_EXCESS_PIPS,
                            "mean_slippage_diff_pips": round(ENTRY_SLIPPAGE_PIPS - cost.slippage_pips, 4),
                            "commission_diff_usd_per_lot": 0.0},
            "expected_closed_trades": len(ordinary) + 6,
        },
        "special_trades": {k: {"effect": v[0], "expected": v[1]} for k, v in SPECIAL_EFFECTS.items()},
        "trades": [{"trade_id": t["trade_id"], "kind": t["kind"], "symbol": t["symbol"], "direction": t["direction"],
                    "decision_time": _iso(t["decision_time"]), "entry_time": _iso(t["entry_time"]),
                    "exit_time": _iso(t["exit_time"]) if t["exit_time"] is not None else None,
                    "stop_pips": t["stop_pips"], "adx": t["adx"], "lots": t["lots"], "r_multiple": t["r_multiple"],
                    "exit_kind": None if t["exit_time"] is None else t["exit_kind"],
                    "iso_week": t["exit_time"].strftime("%G-W%V") if t["exit_time"] is not None else None}
                   for t in trades],
    }
    _dump(out / "manifest.json", manifest)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=3)
    parser.add_argument("--force", action="store_true", help="rebuild when out/trading_state.db already exists")
    args = parser.parse_args(argv)
    out = guard_output_dir(args.out)
    if (out / "trading_state.db").exists() and not args.force:
        print(f"refusing: {out / 'trading_state.db'} exists (use --force to rebuild)")
        return 2
    manifest = build_fixture(out, seed=args.seed, force=args.force)
    counts: dict[str, int] = {}
    for t in manifest["trades"]:
        counts[t["kind"]] = counts.get(t["kind"], 0) + 1
    print(f"Synthetic demo fixture (seed {args.seed}, NOT EVIDENCE): {json.dumps(counts, sort_keys=True)}")
    print(f"Manifest: {out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
