"""C2: unit-aware demo-record readers (R7/R8/R9) and burn-in status for cent (USC) accounts."""
from __future__ import annotations
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from research import demo_records as dr
from research import drift_monitor as dm
from research import fill_reconciliation as fr
from research import trade_postmortem as tp
from research.synthetic_demo import build_fixture
from tests.bridge_fixtures import USC_CONTRACT_SIZE, USC_FACTOR
from validation import burnin_status


@pytest.fixture(scope="module")
def usd_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("c2") / "usd"
    build_fixture(out, seed=3)
    return out


@pytest.fixture(scope="module")
def usc_dir(usd_dir, tmp_path_factory):
    """USC twin of the research fixture: money x100, volume x100, contract size 1,000."""
    dst = tmp_path_factory.mktemp("c2") / "usc"
    shutil.copytree(usd_dir, dst)
    deals = json.loads((dst / "deals.json").read_text(encoding="utf-8"))
    symbols = set()
    for row in deals["deals"]:
        symbols.add(row["symbol"])
        for key in ("profit", "commission", "swap", "fee", "volume"):
            if isinstance(row.get(key), (int, float)):
                row[key] = round(float(row[key]) * USC_FACTOR, 8)
    (dst / "deals.json").write_text(json.dumps(deals), encoding="utf-8")
    for path in (dst / "exits").glob("exit_*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data.get("profit_loss"), (int, float)):
            data["profit_loss"] = round(float(data["profit_loss"]) * USC_FACTOR, 8)
            path.write_text(json.dumps(data), encoding="utf-8")
    conn = sqlite3.connect(dst / "trading_state.db")  # broker lots are x100 on a cent account too
    conn.execute("UPDATE trades SET lot_size = lot_size * ?", (USC_FACTOR,))
    conn.commit()
    conn.close()
    (dst / "symbols.json").write_text(json.dumps(
        {"schema_version": 1, "symbols": {s: {"contract_size": USC_CONTRACT_SIZE} for s in sorted(symbols)}}),
        encoding="utf-8")
    (dst / "status.json").write_text(json.dumps({"account_currency": "USC"}), encoding="utf-8")
    return dst


def _closed(fx: Path, units):
    deals, _ = dr.load_deals(fx / "deals.json", units)
    conn = dr.open_ro(fx / "trading_state.db")
    try:
        return {t["trade_id"]: t for t in dr.closed_trades(conn, deals=deals, units=units)}
    finally:
        conn.close()


def test_resolve_units_flag_sibling_and_default(usd_dir, usc_dir, tmp_path):
    flag = dr.resolve_units(usd_dir / "deals.json", account_currency="USC")
    assert flag.unit_scale == 0.01 and not flag.is_default
    sibling = dr.resolve_units(usc_dir / "deals.json")
    assert sibling.currency == "USC" and sibling.unit_scale == 0.01
    assert set(sibling.contract_sizes.values()) == {USC_CONTRACT_SIZE}
    assert sibling.volume("EURUSD", 100.0) == pytest.approx(1.0)
    assert sibling.money(100.0) == pytest.approx(1.0)
    explicit = dr.resolve_units(usd_dir / "deals.json", symbols_path=usc_dir / "symbols.json")
    assert explicit.unit_scale == 1.0 and not explicit.is_default
    default = dr.resolve_units(usd_dir / "deals.json")
    assert default.is_default and default.currency == "USD" and default.contract_sizes == {}
    assert dr.resolve_units(tmp_path / "missing" / "deals.json").is_default
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "status.json").write_text("{not json", encoding="utf-8")
    (bad / "symbols.json").write_text("[]", encoding="utf-8")
    assert dr.resolve_units(bad / "deals.json").is_default


def test_loaders_convert_cents(usd_dir, usc_dir):
    units = dr.resolve_units(usc_dir / "deals.json")
    usd_deals, _ = dr.load_deals(usd_dir / "deals.json")
    usc_deals, _ = dr.load_deals(usc_dir / "deals.json", units)
    assert len(usd_deals) == len(usc_deals)
    for a, b in zip(usd_deals, usc_deals):
        assert (a.ticket, a.symbol, a.time) == (b.ticket, b.symbol, b.time)
        for field in ("profit", "commission", "swap", "fee", "volume"):
            assert getattr(b, field) == pytest.approx(getattr(a, field), abs=1e-6)
    raw, _ = dr.load_deals(usc_dir / "deals.json")
    assert raw[0].volume == pytest.approx(usd_deals[0].volume * USC_FACTOR)
    usd_exits, usc_exits = dr.load_exits(usd_dir / "exits"), dr.load_exits(usc_dir / "exits", units)
    assert usd_exits.keys() == usc_exits.keys()
    for key, exit_usd in usd_exits.items():
        if isinstance(exit_usd.get("profit_loss"), (int, float)):
            assert usc_exits[key]["profit_loss"] == pytest.approx(exit_usd["profit_loss"], abs=1e-6)


def test_closed_trades_twin_invariants(usd_dir, usc_dir):
    usd = _closed(usd_dir, None)
    usc = _closed(usc_dir, dr.resolve_units(usc_dir / "deals.json"))
    assert usd.keys() == usc.keys()
    compared = 0
    for tid, a in usd.items():
        b = usc[tid]
        for key in ("net_usd", "risk_usd", "net_r"):
            if a[key] is None:
                assert b[key] is None
            else:
                assert b[key] == pytest.approx(a[key], abs=1e-3), (tid, key)
                compared += 1
    assert compared > 30


def test_lot_factor_scales_db_lot_size_fallback():
    trade = {"symbol": "EURUSD", "lot_size": 1.0, "entry_price": 1.1, "stop_loss": 10.0}
    base = dr.net_r_fields(trade, [], [])["risk_usd"]
    assert dr.net_r_fields(trade, [], [], lot_factor=0.01)["risk_usd"] == pytest.approx(base * 0.01, abs=1e-3)


def _r7(fx: Path, out: Path, *extra: str) -> dict:
    assert fr.main(["--db", str(fx / "trading_state.db"), "--deals", str(fx / "deals.json"),
                    "--bars-dir", str(fx / "bars"), "--feedback-dir", str(fx / "feedback"),
                    "--out", str(out), *extra]) == 0
    return json.loads((out / "fill_reconciliation.json").read_text(encoding="utf-8"))


def test_r7_commission_per_lot_matches_and_default_has_no_units(usd_dir, usc_dir, tmp_path):
    usd = _r7(usd_dir, tmp_path / "usd")
    usc = _r7(usc_dir, tmp_path / "usc")
    assert "units" not in usd["inputs"]
    assert usc["inputs"]["units"]["currency"] == "USC"
    assert usc["suggested_adjustment"]["commission_per_lot_usd"] == pytest.approx(
        usd["suggested_adjustment"]["commission_per_lot_usd"], abs=1e-3)
    rows_usd = {r["trade_id"]: r["commission_per_lot_usd"] for r in usd["rows"] if r.get("commission_per_lot_usd") is not None}
    rows_usc = {r["trade_id"]: r["commission_per_lot_usd"] for r in usc["rows"] if r.get("commission_per_lot_usd") is not None}
    assert rows_usd and rows_usd.keys() == rows_usc.keys()
    for tid, value in rows_usd.items():
        assert rows_usc[tid] == pytest.approx(value, abs=1e-3)


def test_r7_flag_overrides_sibling_files(usc_dir, tmp_path):
    flagged = _r7(usc_dir, tmp_path / "f", "--account-currency", "USC", "--symbols", str(usc_dir / "symbols.json"))
    assert flagged["inputs"]["units"]["currency"] == "USC"


def test_r8_live_net_r_twin(usd_dir, usc_dir):
    a = dm.load_live(usd_dir / "trading_state.db", dr.DEFAULT_STREAM, usd_dir / "exits", usd_dir / "deals.json")
    units = dr.resolve_units(usc_dir / "deals.json")
    b = dm.load_live(usc_dir / "trading_state.db", dr.DEFAULT_STREAM, usc_dir / "exits", usc_dir / "deals.json", units)
    assert a[0] == b[0] and len(a[1]) == 35
    assert b[1] == pytest.approx(a[1], abs=1e-3)


def test_r8_main_units_key_only_when_non_default(usd_dir, usc_dir, tmp_path):
    reports = {}
    for name, fx in (("usd", usd_dir), ("usc", usc_dir)):
        out = tmp_path / name
        assert dm.main(["--db", str(fx / "trading_state.db"), "--replay-trades-csv", str(fx / "replay_trades.csv"),
                        "--exits-dir", str(fx / "exits"), "--deals", str(fx / "deals.json"), "--out", str(out)]) == 0
        reports[name] = json.loads((out / "drift_monitor.json").read_text(encoding="utf-8"))
    assert "units" not in reports["usd"]["inputs"]
    assert reports["usc"]["inputs"]["units"]["currency"] == "USC"
    assert reports["usc"]["verdict"] == reports["usd"]["verdict"]
    assert reports["usc"]["n_live"] == reports["usd"]["n_live"]


def _r9(fx: Path):
    return tp.run(fx / "trading_state.db", fx / "deals.json", fx / "bars", exits_dir=fx / "exits",
                  feedback_dir=fx / "feedback", calendar=fx / "economic_calendar.json")


def test_r9_report_twin(usd_dir, usc_dir):
    a, b = _r9(usd_dir), _r9(usc_dir)
    assert not any("units" in s["name"] for s in a["sources"])
    assert any("units" in s["name"] for s in b["sources"])
    assert [t["trade_id"] for t in a["trades"]] == [t["trade_id"] for t in b["trades"]]
    for x, y in zip(a["trades"], b["trades"]):
        assert y["net_r"] == pytest.approx(x["net_r"], abs=1e-3) if x.get("net_r") is not None else y.get("net_r") is None
        assert y["primary_cause"] == x["primary_cause"]


def test_burnin_status_labels_usd(tmp_path, monkeypatch, capsys):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE decision_funnel_events (timestamp TEXT, stage TEXT, reason_code TEXT, outcome TEXT);
        CREATE TABLE trades (trade_id TEXT, symbol TEXT, direction TEXT, status TEXT, execution_time TEXT,
                             profit_loss REAL, r_multiple REAL);
        CREATE TABLE execution_feedback_conflicts (resolved_at TEXT);
        CREATE TABLE trade_reservations (state TEXT);
        CREATE TABLE risk_events (rule_name TEXT, severity TEXT, timestamp TEXT);
        INSERT INTO trades VALUES ('T1', 'EURUSD', 'BUY', 'CLOSED_WIN', '2026-11-04T10:00:00', 1.5, 1.2);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(burnin_status, "DB", db)
    monkeypatch.setattr(burnin_status, "_health", lambda: None)
    assert burnin_status.main([]) == 0
    assert "net P/L (USD)" in capsys.readouterr().out
