"""R9 trade postmortem: planted causes, precedence, calendar coverage, weekly files, small-sample rule, determinism."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pytest

from research import trade_postmortem as tp
from research.demo_records import DEFAULT_THRESHOLDS, load_thresholds
from research.synthetic_demo import build_fixture

CFG = load_thresholds()[0]["r9_postmortem"]
T0 = pd.Timestamp("2026-11-04T10:00:00Z")


@pytest.fixture(scope="module")
def fixture_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("r9") / "fx"
    build_fixture(out, seed=3)
    (out / "status.json").write_text('{"account_currency": "USD"}', encoding="utf-8")  # currency evidence (BR-004)
    return out


def _argv(fx: Path, out: Path, *extra: str) -> list[str]:
    return ["--db", str(fx / "trading_state.db"), "--deals", str(fx / "deals.json"), "--bars-dir", str(fx / "bars"),
            "--exits-dir", str(fx / "exits"), "--feedback-dir", str(fx / "feedback"),
            "--calendar", str(fx / "economic_calendar.json"), "--out", str(out), *extra]


@pytest.fixture(scope="module")
def report(fixture_dir):
    fx = fixture_dir
    return tp.run(fx / "trading_state.db", fx / "deals.json", fx / "bars", exits_dir=fx / "exits",
                  feedback_dir=fx / "feedback", calendar=fx / "economic_calendar.json")


def _by_id(report):
    return {t["trade_id"]: t for t in report["trades"]}


def _causes(trade):
    return [f["cause"] for f in trade["flags"]]


def test_planted_special_trades_get_their_primary_cause(report):
    t = _by_id(report)
    assert t["SYN_EXEC"]["primary_cause"] == "EXECUTION"
    assert any("after bar close" in f["evidence"] for f in t["SYN_EXEC"]["flags"])
    assert t["SYN_MANUAL"]["primary_cause"] == "EXECUTION"
    assert any("not placed by the EA (magic 0," in f["evidence"] for f in t["SYN_MANUAL"]["flags"])
    assert t["SYN_NEWS"]["primary_cause"] == "NEWS_CALENDAR"
    assert t["SYN_COST"]["primary_cause"] == "SPREAD_COST"
    assert t["SYN_COST"]["cost_r"] >= CFG["cost_r_fraction"]
    assert t["SYN_REGIME"]["primary_cause"] == "REGIME_MISMATCH"
    assert any("regime changed during trade" in f["evidence"] for f in t["SYN_REGIME"]["flags"])
    missing = t["SYN_MISSING"]
    assert any(n.startswith("INSUFFICIENT_DATA") for n in missing["notes"])
    assert missing["primary_cause"] not in ("EXECUTION", "SPREAD_COST")
    assert not {"EXECUTION", "SPREAD_COST"} & set(_causes(missing))
    assert "SYN_OPEN" not in t and "SYN_MATRIX" not in t


def test_every_ordinary_trade_is_ordinary_noise(report):
    ordinary = [t for t in report["trades"] if t["trade_id"].startswith("AI_SYN_")]
    assert len(ordinary) == 30
    assert {t["primary_cause"] for t in ordinary} == {"ORDINARY_NOISE"}
    assert all(t["flags"] == [] for t in ordinary)


def _hand_trade(**over):
    trade = {"trade_id": "HAND", "symbol": "EURUSD", "direction": "BUY", "status": "CLOSED_LOSS", "source": "db",
             "decision_time": T0 + pd.Timedelta(seconds=3), "entry_time": T0 + pd.Timedelta(seconds=120),
             "exit_time": T0 + pd.Timedelta(hours=1), "net_r": -1.0, "profit_loss": -320.0, "risk_usd": 320.0,
             "stop_pips": 20.0,
             "stop_source": "funnel TECHNICAL stop_pips", "prep_notes": [],
             "r7": {"comparable": True, "fill_diff_pips": 0.2, "fill_latency_s": 120.0, "exit_slippage_diff_pips": 0.0,
                    "exit_slippage_pips": 0.0, "exit_kind": "STOP", "actual_spread_pips": 1.0,
                    "implied_slippage_pips": 0.2, "commission_per_lot_usd": 7.0, "volume": 1.0}}
    trade.update(over)
    return trade


def _events(*items):
    return [{"time": pd.Timestamp(ts), "currency": cur, "impact": imp, "event": name} for ts, cur, imp, name in items]


COVER = (("2026-11-01T00:00:00Z", "USD", "low", "start"), ("2026-11-20T00:00:00Z", "USD", "low", "end"))


def test_precedence_keeps_all_flags_and_picks_execution():
    events = _events(*COVER, ("2026-11-04T10:20:00Z", "EUR", "high", "ECB"))
    out = tp.classify_trade(_hand_trade(), [], events, {"conflicts": set(), "uncertain": set()}, CFG)
    assert out["primary_cause"] == "EXECUTION"
    assert "EXECUTION" in _causes(out) and "NEWS_CALENDAR" in _causes(out)
    assert not any(n.startswith("NEWS_NOT_EVALUABLE") for n in out["notes"])
    # 1.2 pip / 20 + 7 USD / 320 USD = 0.081875 R
    assert out["cost_r"] == pytest.approx(0.0819, abs=1e-4)


def test_all_day_event_blackout_override_flags_news():
    # Like the repo calendar's time_exact false events: nominal time far from the trade, 720-minute overrides
    allday = {"time": pd.Timestamp("2026-11-04T00:00:00Z"), "currency": "EUR", "impact": "high",
              "event": "Leaders summit", "time_exact": False, "blackout_before_minutes": 720,
              "blackout_after_minutes": 720}
    events = [*_events(*COVER), allday]
    trade = _hand_trade(r7={"comparable": True}, entry_time=T0 + pd.Timedelta(seconds=6))
    out = tp.classify_trade(trade, [], events, {}, CFG)
    assert out["primary_cause"] == "NEWS_CALENDAR"
    assert "time not exact" in out["flags"][0]["evidence"]
    late = _hand_trade(r7={"comparable": True}, entry_time=T0 + pd.Timedelta(hours=3),
                       exit_time=T0 + pd.Timedelta(hours=4))  # 13:00-14:00, after the 12:00 end of the blackout
    assert "NEWS_CALENDAR" not in _causes(tp.classify_trade(late, [], events, {}, CFG))
    plain = [*_events(*COVER), {k: v for k, v in allday.items() if not k.startswith(("blackout", "time_exact"))}]
    assert "NEWS_CALENDAR" not in _causes(tp.classify_trade(trade, [], plain, {}, CFG))


def test_trade_outside_calendar_coverage_gets_not_evaluable_note():
    events = _events(("2026-12-01T00:00:00Z", "USD", "high", "later"), ("2026-12-05T00:00:00Z", "USD", "low", "end"))
    out = tp.classify_trade(_hand_trade(r7={"comparable": True}), [], events, {}, CFG)
    assert "NEWS_CALENDAR" not in _causes(out)
    assert any(n.startswith("NEWS_NOT_EVALUABLE") for n in out["notes"])


def test_open_issue_flags_execution():
    trade = _hand_trade(r7=None, entry_time=T0 + pd.Timedelta(seconds=6))
    out = tp.classify_trade(trade, [], _events(*COVER), {"conflicts": set(), "uncertain": {"HAND"}}, CFG)
    assert out["primary_cause"] == "EXECUTION"
    assert any("INSUFFICIENT_DATA: no R7 row" in n for n in out["notes"])


def test_borderline_adx_only_for_losers():
    row = {"symbol": "EURUSD", "stage": "REGIME", "_t": T0 + pd.Timedelta(seconds=3), "reason_code": "REGIME_TREND_BULL",
           "details": "regime=TRENDING_BULL adx=21.00 adx_transition_below=20.00"}
    base = dict(r7={"comparable": True}, entry_time=T0 + pd.Timedelta(seconds=6))
    loser = tp.classify_trade(_hand_trade(**base), [row], _events(*COVER), {}, CFG)
    assert loser["primary_cause"] == "REGIME_MISMATCH"
    assert "borderline trend at entry" in loser["flags"][0]["evidence"]
    winner = tp.classify_trade(_hand_trade(net_r=2.2, profit_loss=704.0, **base), [row], _events(*COVER), {}, CFG)
    assert winner["primary_cause"] == "ORDINARY_NOISE"
    no_row = tp.classify_trade(_hand_trade(**base), [], _events(*COVER), {}, CFG)
    assert "INSUFFICIENT_DATA: no entry REGIME funnel row" in no_row["notes"]


def test_weekly_files_and_pattern_section(fixture_dir, tmp_path, capsys):
    out = tmp_path / "r9"
    assert tp.main(_argv(fixture_dir, out)) == 0
    assert sorted(p.name for p in out.iterdir()) == ["postmortem.json", "postmortem_2026-W45.md", "postmortem_2026-W46.md"]
    w45 = (out / "postmortem_2026-W45.md").read_text(encoding="utf-8")
    assert w45.startswith("# Trade postmortem 2026-W45 (R9)")
    assert tp.REPORT_ONLY in w45 and "## Patterns" in w45 and "INCONCLUSIVE" not in w45
    data = json.loads((out / "postmortem.json").read_text(encoding="utf-8"))
    assert data["weeks"]["2026-W45"]["n_trades"] + data["weeks"]["2026-W46"]["n_trades"] == 36
    assert data["weeks"]["2026-W46"]["funnel_rejections"] == [
        {"stage": "REGIME", "reason_code": "REGIME_TRANSITION", "count": 1}]
    assert "Outputs:" in capsys.readouterr().out


def test_small_week_is_inconclusive_without_patterns(fixture_dir, tmp_path):
    config = json.loads(DEFAULT_THRESHOLDS.read_text(encoding="utf-8"))
    config["r9_postmortem"]["min_trades_for_pattern"] = 50
    th = tmp_path / "th.json"
    th.write_text(json.dumps(config), encoding="utf-8")
    out = tmp_path / "r9"
    assert tp.main(_argv(fixture_dir, out, "--thresholds", str(th), "--week", "2026-W46")) == 0
    assert sorted(p.name for p in out.iterdir()) == ["postmortem.json", "postmortem_2026-W46.md"]
    md = (out / "postmortem_2026-W46.md").read_text(encoding="utf-8")
    assert "INCONCLUSIVE: 16 closed trades; no pattern claims" in md
    assert "## Patterns" not in md


def test_two_runs_identical_apart_from_metadata(fixture_dir, tmp_path):
    docs = []
    for name in ("a", "b"):
        assert tp.main(_argv(fixture_dir, tmp_path / name)) == 0
        doc = json.loads((tmp_path / name / "postmortem.json").read_text(encoding="utf-8"))
        assert set(doc.pop("metadata")) == {"run_utc"}
        docs.append(doc)
    assert docs[0] == docs[1]
    assert "account_scope" not in json.dumps(docs[0])
    assert (tmp_path / "a" / "postmortem_2026-W45.md").read_text() == (tmp_path / "b" / "postmortem_2026-W45.md").read_text()


def test_default_calendar_recorded_as_project_relative_path(fixture_dir):
    fx = fixture_dir
    rep = tp.run(fx / "trading_state.db", fx / "deals.json", fx / "bars", exits_dir=fx / "exits")
    cal = [s for s in rep["sources"] if s["name"].startswith("economic calendar")]
    assert [s["path"] for s in cal] == ["data/economic_calendar.json"]


def test_module_has_no_llm_or_network_imports():
    source = Path(tp.__file__).read_text(encoding="utf-8")
    pattern = re.compile(r"^\s*(import|from)\s+(openai|anthropic|requests|urllib)\b", re.MULTILINE)
    assert pattern.search(source) is None
