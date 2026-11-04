"""Wave 3 demo-record loaders and synthetic fixture: read-only access, guards, parsing, determinism."""
from __future__ import annotations

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
