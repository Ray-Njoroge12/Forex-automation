"""R7 fill reconciliation: exact per-deal measures, planted fixture bias, small-sample fail-closed, cost model untouched."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

import pandas as pd
import pytest

from backtesting.unified_replay import COST_SCENARIOS, DEFAULT_SPREAD_FLOOR_PIPS
from research import fill_reconciliation as fr
from research.common import PROJECT_ROOT
from research.demo_records import (
    DEFAULT_STREAM, EA_MAGIC, Deal, load_deals, load_feedback_files, load_m15, load_thresholds, open_ro,
)
from research.synthetic_demo import build_fixture

R7 = load_thresholds()[0]["r7_fill_reconciliation"]
T0 = pd.Timestamp("2026-11-02T10:00:00Z")


def _deal(ticket, position, time, side, entry, price, comment, magic=EA_MAGIC):
    return Deal(ticket=ticket, order=position, position_id=position, time=pd.Timestamp(time), type=side, entry=entry,
                symbol="EURUSD", volume=1.0, price=price, profit=0.0, commission=-3.5, swap=0.0, fee=0.0,
                magic=magic, comment=comment)


def _hand_built_db(path):
    conn = sqlite3.connect(path)
    conn.executescript((PROJECT_ROOT / "database" / "schema.sql").read_text(encoding="utf-8"))
    for tid, ticket, pos, direction, entry, decision, fill, spread, slip in (
        ("T_BUY", 101, 201, "BUY", 1.10023, "2026-11-02T10:15:03+00:00", "2026-11-02T10:15:06Z", 0.0001, 0.00003),
        ("T_SELL", 102, 202, "SELL", 1.09995, "2026-11-02T10:00:02+00:00", "2026-11-02T10:00:30Z", 0.00012, 0.00005),
    ):
        conn.execute("INSERT INTO trades (trade_id, trade_ticket, position_ticket, evidence_stream, symbol, direction,"
                     " lot_size, entry_price, status, open_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (tid, ticket, pos, DEFAULT_STREAM, "EURUSD", direction, 1.0, entry, "CLOSED_LOSS", decision))
        conn.execute("INSERT INTO decision_funnel_events (evidence_stream, decision_time, symbol, stage, outcome,"
                     " reason_code, details, trade_id) VALUES (?,?,?,?,?,?,?,?)",
                     (DEFAULT_STREAM, decision, "EURUSD", "ROUTER", "ROUTED", "ROUTED_TO_MT5", "", tid))
        payload = {"entry_price": entry, "spread_at_entry": spread, "slippage": slip, "ticket": ticket,
                   "position_ticket": pos, "close_time": fill}
        conn.execute("INSERT INTO trade_lifecycle_events (trade_id, event_type, status, reason_code, payload_json,"
                     " created_at) VALUES (?,?,?,?,?,?)",
                     (tid, "EXECUTION", "EXECUTED_OPEN", "EXECUTED", json.dumps(payload), fill))
    conn.commit()
    conn.close()


def _three_bars():
    idx = pd.DatetimeIndex([T0, T0 + pd.Timedelta(minutes=15), T0 + pd.Timedelta(minutes=30)])
    opens = [1.10000, 1.10010, 1.10020]
    return {"EURUSD": pd.DataFrame({"open": opens, "high": [o + 0.0005 for o in opens],
                                    "low": [o - 0.0005 for o in opens], "close": opens,
                                    "volume": [100.0] * 3, "spread_points": [3.0] * 3}, index=idx)}


def _fixture(tmp_path):
    out = tmp_path / "fx"
    build_fixture(out, seed=3)
    (out / "status.json").write_text('{"account_currency": "USD"}', encoding="utf-8")  # currency evidence (BR-004)
    return out


def _fixture_rows(out):
    deals, _ = load_deals(out / "deals.json")
    conn = open_ro(out / "trading_state.db")
    try:
        return fr.reconcile(conn, deals, load_m15(out / "bars"), feedback_files=load_feedback_files(out / "feedback"))
    finally:
        conn.close()


def test_hand_built_buy_and_sell_measures_are_exact(tmp_path):
    db = tmp_path / "s.db"
    _hand_built_db(db)
    deals = [
        _deal(102, 202, "2026-11-02T10:00:30Z", 1, 0, 1.09995, "T_SELL"),
        _deal(101, 201, "2026-11-02T10:15:06Z", 0, 0, 1.10023, "T_BUY"),
        _deal(103, 201, "2026-11-02T10:40:00Z", 1, 1, 1.09996, "[sl 1.10000]"),
        _deal(104, 202, "2026-11-02T10:50:00Z", 0, 1, 1.09900, "[tp 1.09900]"),
    ]
    conn = open_ro(db)
    try:
        rows = {r["trade_id"]: r for r in fr.reconcile(conn, deals, _three_bars())}
    finally:
        conn.close()
    buy, sell = rows["T_BUY"], rows["T_SELL"]
    assert buy["comparable"] and sell["comparable"]
    assert buy["decision_time_source"] == "ROUTER"
    assert buy["modelled_spread_pips"] == pytest.approx(0.8)
    assert buy["replay_fill"] == pytest.approx(1.10020)
    assert buy["fill_diff_pips"] == pytest.approx(0.3)
    assert buy["spread_diff_pips"] == pytest.approx(0.2)
    assert buy["implied_slippage_pips"] == pytest.approx(0.3)
    assert buy["slippage_diff_pips"] == pytest.approx(0.1)
    assert buy["ea_slippage_pips"] == pytest.approx(0.3)
    assert (buy["signal_latency_s"], buy["fill_latency_s"], buy["route_to_fill_s"]) == pytest.approx((3.0, 6.0, 3.0))
    assert buy["strict_fill"] == pytest.approx(1.10030)
    assert buy["strict_diff_pips"] == pytest.approx(-0.7)
    assert buy["exit_kind"] == "STOP" and buy["exit_level"] == pytest.approx(1.10000)
    assert buy["exit_slippage_pips"] == pytest.approx(0.4)
    assert buy["exit_slippage_diff_pips"] == pytest.approx(0.2)
    assert buy["commission_per_lot_usd"] == pytest.approx(7.0)
    assert buy["commission_diff_usd_per_lot"] == pytest.approx(0.0)
    assert buy["price_mismatch_pips"] is None
    # live 1.0 + 0.3 + 0.4 + 0.7 vs model 0.8 + 0.2 + 0.2 + 0.7 (7 USD / 10 USD per pip)
    assert buy["cost_ratio"] == pytest.approx(2.4 / 1.9)

    assert sell["replay_fill"] == pytest.approx(1.09998)
    assert sell["fill_diff_pips"] == pytest.approx(0.3)
    assert sell["spread_diff_pips"] == pytest.approx(0.4)
    assert sell["implied_slippage_pips"] == pytest.approx(0.5)
    assert sell["slippage_diff_pips"] == pytest.approx(0.3)
    assert (sell["signal_latency_s"], sell["fill_latency_s"], sell["route_to_fill_s"]) == pytest.approx((2.0, 30.0, 28.0))
    assert sell["exit_kind"] == "TARGET"
    assert sell["exit_slippage_pips"] == pytest.approx(0.0)
    assert sell["exit_slippage_diff_pips"] == pytest.approx(0.0)


def test_fixture_planted_bias_is_recovered(tmp_path):
    rows = _fixture_rows(_fixture(tmp_path))
    report = fr.summarise(rows, R7, COST_SCENARIOS["base"])
    m = report["metrics"]
    assert m["spread_diff_pips"]["overall"]["mean"] == pytest.approx(0.5, abs=0.05)
    assert m["slippage_diff_pips"]["overall"]["mean"] == pytest.approx(0.1, abs=0.05)
    assert m["commission_diff_usd_per_lot"]["overall"]["mean"] == pytest.approx(0.0, abs=0.01)
    assert report["verdict"] == "SYSTEMATIC_BIAS"
    assert m["spread_diff_pips"]["overall"]["status"] == "SYSTEMATIC_BIAS"
    by_id = {r["trade_id"]: r for r in rows}
    assert by_id["SYN_MATRIX"]["comparable"] is False and by_id["SYN_MATRIX"]["reason"] == "NO_STRATEGY_SIGNAL"
    assert by_id["SYN_MISSING"]["comparable"] is False and by_id["SYN_MISSING"]["reason"] == "NO_DEAL"
    assert by_id["SYN_MANUAL"]["exit_kind"] == "NOT_BY_EA" and by_id["SYN_MANUAL"]["exit_slippage_pips"] is None
    adj = report["suggested_adjustment"]
    assert adj["actionable"] is True
    assert adj["spread_floor_pips"]["EURUSD"] > DEFAULT_SPREAD_FLOOR_PIPS["EURUSD"]
    assert adj["spread_floor_pips"]["EURUSD"] > adj["current"]["spread_floor_pips"]["EURUSD"]
    assert adj["slippage_pips"] == pytest.approx(0.3, abs=1e-6)


def test_small_sample_is_inconclusive_and_not_actionable(tmp_path):
    rows = _fixture_rows(_fixture(tmp_path))
    few = [r for r in rows if r["comparable"]][: int(R7["min_deals_for_bias"]) - 1]
    report = fr.summarise(few, R7, COST_SCENARIOS["base"])
    assert report["verdict"] == "INCONCLUSIVE"
    assert all(m["overall"]["status"] == "INCONCLUSIVE" for m in report["metrics"].values())
    assert report["cost_ratio"]["status"] == "INCONCLUSIVE"
    assert report["suggested_adjustment"]["actionable"] is False


def test_unbiased_costs_give_no_systematic_bias_despite_fill_latency():
    n = int(R7["min_deals_for_bias"]) + 4
    rows = []
    for i in range(n):
        e = 0.1 if i % 2 == 0 else -0.1  # zero-mean errors around the model
        rows.append({"comparable": True, "symbol": "EURUSD", "fill_diff_pips": e, "spread_diff_pips": -e,
                     "slippage_diff_pips": e, "exit_slippage_diff_pips": -e, "commission_diff_usd_per_lot": e,
                     "fill_latency_s": 6.0 + (i % 3), "implied_slippage_pips": 0.2 + e, "actual_spread_pips": 0.8,
                     "commission_per_lot_usd": 7.0 + e, "cost_ratio": 1.0})
    report = fr.summarise(rows, R7, COST_SCENARIOS["base"])
    assert report["metrics"]["fill_latency_s"]["overall"]["status"] == "SYSTEMATIC_BIAS"
    assert all(report["metrics"][m]["overall"]["status"] == "NO_SYSTEMATIC_BIAS" for m in fr.VERDICT_METRICS)
    assert "fill_latency_s" not in fr.VERDICT_METRICS
    assert report["verdict"] == "NO_SYSTEMATIC_BIAS"
    assert report["suggested_adjustment"]["actionable"] is True
    md = fr.render_markdown(report)
    assert "SYSTEMATIC_BIAS (informational)" in md


def test_exit_magic_is_recorded(tmp_path):
    rows = {r["trade_id"]: r for r in _fixture_rows(_fixture(tmp_path))}
    assert rows["SYN_MANUAL"]["exit_magic"] == 0
    assert rows["AI_SYN_000"]["exit_magic"] == EA_MAGIC


def test_cost_model_is_unchanged(tmp_path):
    before = {k: asdict(v) for k, v in COST_SCENARIOS.items()}
    floors_before = dict(DEFAULT_SPREAD_FLOOR_PIPS)
    report = fr.summarise(_fixture_rows(_fixture(tmp_path)), R7, COST_SCENARIOS["base"])
    report["suggested_adjustment"]["current"]["spread_floor_pips"]["EURUSD"] = 99.0
    assert {k: asdict(v) for k, v in COST_SCENARIOS.items()} == before
    assert DEFAULT_SPREAD_FLOOR_PIPS == floors_before


def test_main_writes_outputs_and_refuses_bridge_folder(tmp_path, capsys):
    fx = _fixture(tmp_path)
    args = ["--db", str(fx / "trading_state.db"), "--deals", str(fx / "deals.json"), "--bars-dir", str(fx / "bars"),
            "--feedback-dir", str(fx / "feedback")]
    out = tmp_path / "r7"
    assert fr.main(args + ["--out", str(out)]) == 0
    for name in ("fill_reconciliation.json", "fill_reconciliation.md", "deals.csv"):
        assert (out / name).is_file()
    md = (out / "fill_reconciliation.md").read_text(encoding="utf-8")
    assert md.startswith("# Fill reconciliation (R7)")
    assert fr.REPORT_ONLY in md
    data = json.loads((out / "fill_reconciliation.json").read_text(encoding="utf-8"))
    assert data["verdict"] == "SYSTEMATIC_BIAS"
    assert len(data["inputs"]["deals_sha256_16"]) == 16 and len(data["inputs"]["thresholds_sha256_16"]) == 16
    assert data["inputs"]["max_trades_id_read"] == 37
    assert "account_scope" not in (out / "fill_reconciliation.json").read_text(encoding="utf-8")
    assert "account_scope" not in (out / "deals.csv").read_text(encoding="utf-8")
    assert "Outputs:" in capsys.readouterr().out
    bridge = tmp_path / "Wine" / "MQL5" / "Files" / "bridge" / "r7"
    with pytest.raises(ValueError):
        fr.main(args + ["--out", str(bridge)])
    assert not bridge.exists()
