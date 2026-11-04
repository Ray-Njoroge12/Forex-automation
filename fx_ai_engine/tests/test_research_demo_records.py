"""Wave 3 demo-record loaders and synthetic fixture: read-only access, guards, parsing, determinism."""
from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from core.filters import calendar_filter
from research import demo_records as dr
from research import synthetic_demo
from validation import validate_demo

DEMO_EVIDENCE = Path(__file__).parents[1] / "artifacts" / "demo-evidence" / "2026-11-03" / "run_160754"
LIVE_REGIME = ("regime=TRENDING_BULL trend_state=BULLISH volatility=NORMAL direction_candidate=BUY close=1.34144 "
               "ema_fast=1.34043 ema_slow=1.33684 adx=20.86 adx_no_trade_below=15.00 adx_transition_below=20.00 "
               "trend_distance_pips=35.88 trend_distance_threshold_pips=2.00 atr_ratio=0.9354 "
               "experiment_pair_selective_rising_adx_relax=0")


@pytest.fixture(scope="module")
def fixture_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("wave3-fixture")
    synthetic_demo.build_fixture(out, seed=3)
    return out


def _small_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
    conn.commit()
    conn.close()
    return path


def test_open_ro_rejects_writes_and_missing_files(tmp_path):
    db = _small_db(tmp_path / "s.db")
    conn = dr.open_ro(db)
    assert [r["x"] for r in conn.execute("SELECT x FROM t ORDER BY x")] == [1, 2, 3]
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t VALUES (4)")
    conn.close()
    with pytest.raises(FileNotFoundError):
        dr.open_ro(tmp_path / "missing.db")


def test_snapshot_db_copies_rows_and_refuses_self(tmp_path):
    db = _small_db(tmp_path / "s.db")
    copy = dr.snapshot_db(db, tmp_path / "snap" / "copy.db")
    conn = sqlite3.connect(copy)
    assert conn.execute("SELECT count(*), sum(x) FROM t").fetchone() == (3, 6)
    conn.close()
    with pytest.raises(ValueError):
        dr.snapshot_db(db, tmp_path / "." / "s.db")


def test_guard_output_dir_rejects_bridge_folder(tmp_path):
    with pytest.raises(ValueError):
        dr.guard_output_dir(tmp_path / "MetaTrader 5" / "MQL5" / "Files" / "bridge" / "out")
    assert dr.guard_output_dir(tmp_path) == tmp_path.resolve()
    assert dr.guard_output_dir(tmp_path / "MQL5" / "Files" / "other") == (tmp_path / "MQL5" / "Files" / "other").resolve()


def test_load_thresholds_version_hash_and_missing_key(tmp_path):
    cfg, digest = dr.load_thresholds()
    assert cfg["version"] == 1
    assert re.fullmatch(r"[0-9a-f]{16}", digest)
    broken = {k: v for k, v in cfg.items() if k != "r8_drift_monitor"}
    path = tmp_path / "t.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ValueError, match="r8_drift_monitor"):
        dr.load_thresholds(path)


def test_thresholds_track_engine_constants():
    cfg, _ = dr.load_thresholds()
    assert cfg["r8_drift_monitor"]["min_live_trades"] == validate_demo.MIN_TRADES
    assert cfg["r9_postmortem"]["news_before_entry_minutes"] == calendar_filter.BLACKOUT_BEFORE_MINUTES
    assert cfg["r9_postmortem"]["news_after_exit_minutes"] == calendar_filter.BLACKOUT_AFTER_MINUTES


def test_parse_details_on_live_regime_format():
    d = dr.parse_details(LIVE_REGIME)
    assert d["regime"] == "TRENDING_BULL"
    assert float(d["adx"]) == pytest.approx(20.86)
    assert float(d["adx_transition_below"]) == pytest.approx(20.0)
    assert dr.parse_details("No adversarial blocker triggered") == {}
    assert dr.parse_details("a=b=c x") == {"a": "b=c"}


def test_load_deals_time_is_utc_and_schema_checked(tmp_path):
    deal = {"ticket": 1, "order": 2, "position_id": 2, "time_msc": 1793723405422, "type": 0, "entry": 0,
            "symbol": "GBPUSD", "volume": 1.0, "price": 1.34175, "profit": 0.0, "commission": -3.5, "swap": 0.0,
            "fee": 0.0, "magic": dr.EA_MAGIC, "comment": "X"}
    path = tmp_path / "deals.json"
    path.write_text(json.dumps({"schema_version": 1, "generated_at": "g", "server_utc_offset_seconds": 7200,
                                "deals": [deal]}), encoding="utf-8")
    deals, meta = dr.load_deals(path)
    assert deals[0].time == pd.Timestamp("2026-11-03T16:30:05.422Z")
    assert meta == {"generated_at": "g", "server_utc_offset_seconds": 7200}
    path.write_text(json.dumps({"schema_version": 2, "deals": [deal]}), encoding="utf-8")
    with pytest.raises(ValueError):
        dr.load_deals(path)


def test_load_feedback_files_prefers_non_zero_ticket():
    fb = dr.load_feedback_files(DEMO_EVIDENCE)
    valid = fb["DEMO_MATRIX_VALID"]
    assert valid["ticket"] == 58758036737
    assert Path(valid["source_file"]).parent.name == "valid"
    assert valid["payload"]["symbol"] == "EURUSD"
    assert dr.load_feedback_files(DEMO_EVIDENCE / "missing") == {}
    assert list(dr.load_exits(DEMO_EVIDENCE / "valid")) == [59159531410]  # exit_result.json skipped


def test_closed_trades_on_fixture(fixture_dir):
    conn = dr.open_ro(fixture_dir / "trading_state.db")
    closed = dr.closed_trades(conn)
    ids = {t["trade_id"] for t in closed}
    assert len(closed) == 36
    assert {"SYN_EXEC", "SYN_NEWS", "SYN_COST", "SYN_REGIME", "SYN_MANUAL", "SYN_MISSING"} <= ids
    assert "SYN_OPEN" not in ids and "SYN_MATRIX" not in ids
    assert sum(t["trade_id"].startswith("AI_SYN_") for t in closed) == 30
    assert all(t["source"] == "db" for t in closed)
    conn.close()


def test_closed_trades_uses_exit_copy_for_open_trade(fixture_dir, tmp_path):
    conn = dr.open_ro(fixture_dir / "trading_state.db")
    deals, _ = dr.load_deals(fixture_dir / "deals.json")
    open_trade = next(t for t in dr.load_trades(conn) if t["trade_id"] == "SYN_OPEN")
    pos = open_trade["position_ticket"]
    entry = next(d for d in deals if d.position_id == pos and d.entry == 0)
    exit_time = entry.time + pd.Timedelta(minutes=47, milliseconds=520)
    exit_deal = dr.Deal(ticket=entry.ticket + 1, order=entry.order, position_id=pos, time=exit_time, type=1,
                        entry=1, symbol=entry.symbol, volume=entry.volume, price=entry.price, profit=12.0,
                        commission=entry.commission, swap=0.0, fee=0.0, magic=0, comment="")
    exits = {pos: {"position_ticket": pos, "trade_id": "SYN_OPEN", "status": "CLOSED_WIN", "is_final_exit": True,
                   "r_multiple": 0.49, "profit_loss": 150.0,
                   "close_time": (exit_time.floor("s") + pd.Timedelta(seconds=7200)).strftime("%Y-%m-%dT%H:%M:%SZ")}}
    closed = dr.closed_trades(conn, exits=exits, deals=[*deals, exit_deal])
    item = next(t for t in closed if t["trade_id"] == "SYN_OPEN")
    assert item["source"] == "bridge_exit_copy"
    assert item["status"] == "CLOSED_WIN"
    assert item["r_multiple"] == pytest.approx(0.49)
    assert item["exit_time"] == exit_time
    assert item["exit_time_unverified"] is False
    assert item["exit_file_close_time_offset_s"] == 7200
    without_deal = next(t for t in dr.closed_trades(conn, exits=exits, deals=deals) if t["trade_id"] == "SYN_OPEN")
    assert without_deal["exit_time_unverified"] is True
    assert len(closed) == 37
    conn.close()


def test_closed_trades_net_r_differs_from_ea_gross_r_by_commission(fixture_dir):
    conn = dr.open_ro(fixture_dir / "trading_state.db")
    deals, _ = dr.load_deals(fixture_dir / "deals.json")
    closed = {t["trade_id"]: t for t in dr.closed_trades(conn, deals=deals)}
    conn.close()
    planted = {t["trade_id"]: t for t in json.loads((fixture_dir / "manifest.json").read_text())["trades"]}
    ordinary = [t for tid, t in closed.items() if tid.startswith("AI_SYN_")]
    for t in ordinary:
        lots, stop = planted[t["trade_id"]]["lots"], planted[t["trade_id"]]["stop_pips"]
        assert t["risk_usd"] == pytest.approx(lots * stop * 10.0)  # EURUSD/GBPUSD pip value 10 USD per lot
        planted_pl = round(planted[t["trade_id"]]["r_multiple"] * 320.0, 2)
        # the fixture charges 3.50 USD per lot per deal; the EA-style stored R leaves it out, net R includes it
        assert t["r_multiple"] == pytest.approx((planted_pl + 2 * round(3.5 * lots, 2)) / 320.0, abs=1e-4)
        assert t["net_r"] == pytest.approx(planted_pl / t["risk_usd"], abs=1e-4)
        assert t["r_multiple"] > t["net_r"]
        assert t["net_r"] == pytest.approx(planted[t["trade_id"]]["r_multiple"], rel=5e-3)
    # SYN_MISSING has no deal: no net R, and its trades.close_time fallback is unverified (EA server time)
    missing = closed["SYN_MISSING"]
    assert missing["net_r"] is None and missing["net_r_missing"] == "no closing deal"
    assert missing["exit_time_source"] == "trades.close_time" and missing["exit_time_unverified"] is True
    assert closed["AI_SYN_000"]["exit_time_source"] == "deal" and closed["AI_SYN_000"]["exit_time_unverified"] is False


def test_closed_trades_uses_final_deal_after_partial_close(fixture_dir):
    conn = dr.open_ro(fixture_dir / "trading_state.db")
    deals, _ = dr.load_deals(fixture_dir / "deals.json")
    trade = next(t for t in dr.load_trades(conn) if t["trade_id"] == "AI_SYN_000")
    pos = trade["position_ticket"]
    final = next(d for d in deals if d.position_id == pos and d.entry == 1)
    # EA-style 50% partial close 10 minutes before the final exit; the final deal keeps the other half
    half = round(final.volume / 2, 2)
    partial = dr.Deal(ticket=final.ticket - 10_000, order=final.order, position_id=pos,
                      time=final.time - pd.Timedelta(minutes=10), type=final.type, entry=1, symbol=final.symbol,
                      volume=half, price=final.price, profit=100.0, commission=-1.0, swap=-0.5, fee=0.0,
                      magic=dr.EA_MAGIC, comment="partial")
    rest = [d for d in deals if d.ticket != final.ticket]
    final_rest = dataclasses.replace(final, volume=round(final.volume - half, 2), profit=50.0, commission=-2.0)
    entry = next(d for d in deals if d.position_id == pos and d.entry == 0)
    closed = {t["trade_id"]: t for t in dr.closed_trades(conn, deals=[*rest, partial, final_rest])}
    conn.close()
    item = closed["AI_SYN_000"]
    assert item["exit_time"] == final.time
    assert item["closing_deals"] == 2
    net = entry.commission + 100.0 - 1.0 - 0.5 + 50.0 - 2.0
    assert item["net_usd"] == pytest.approx(net)
    assert item["net_r"] == pytest.approx(net / item["risk_usd"], abs=1e-6)


def test_exit_copy_without_r_multiple_keeps_none(fixture_dir):
    conn = dr.open_ro(fixture_dir / "trading_state.db")
    trade = next(t for t in dr.load_trades(conn) if t["trade_id"] == "SYN_OPEN")
    pos = trade["position_ticket"]
    exits = {pos: {"position_ticket": pos, "status": "CLOSED_WIN", "is_final_exit": True,
                   "close_time": "2026-11-13T12:00:00Z"}}
    item = next(t for t in dr.closed_trades(conn, exits=exits) if t["trade_id"] == "SYN_OPEN")
    conn.close()
    assert item["r_multiple"] is None and item["profit_loss"] is None
    assert item["exit_time_source"] == "exit_file.close_time" and item["exit_time_unverified"] is True
    assert item["net_r"] is None


def test_fixture_win_r_is_the_policy_minimum_rr():
    from config_microcapital import get_policy_config
    assert synthetic_demo.WIN_R == float(get_policy_config(mode_id="core_srs", env={})["MIN_RISK_REWARD"])


def test_fixture_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    synthetic_demo.build_fixture(a, seed=3)
    synthetic_demo.build_fixture(b, seed=3)
    for name in ("deals.json", "replay_trades.csv", "manifest.json", "economic_calendar.json",
                 "bars/bars_EURUSD_M15.json", "exits/exit_59200000000.json"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    dumps = []
    for d in (a, b):
        conn = sqlite3.connect(d / "trading_state.db")
        dumps.append(list(conn.iterdump()))
        conn.close()
    assert dumps[0] == dumps[1]
    with pytest.raises(FileExistsError):
        synthetic_demo.build_fixture(a, seed=3)
    assert synthetic_demo.main(["--out", str(a)]) == 2
