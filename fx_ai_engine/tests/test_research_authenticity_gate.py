"""R2 authenticity gate: verdict ordering and labelling, fill checks on synthetic trades, fixture end to end."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtesting.unified_replay import COST_SCENARIOS, Candidate, CostModel, SimulationConfig, normalise_frame, simulate
from config_microcapital import get_policy_config
from research import authenticity_gate as gate
from research.common import load_frames, policy_for_variant, run_replay_in_process

FIXTURE = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
POLICY = get_policy_config(mode_id="core_srs", env={})
COST = CostModel(slippage_pips=0.0, commission_per_lot_usd=0.0, spread_floor_pips={"EURUSD": 1.0})


def _frame(start: str, periods: int, freq: str, prices: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return normalise_frame(pd.DataFrame({"open": prices, "high": prices + 0.0005, "low": prices - 0.0005,
                                         "close": prices, "volume": 1, "spread_points": 0}, index=idx))


def _candidate(t: pd.Timestamp) -> Candidate:
    signal = dict(trade_id="T", symbol="EURUSD", direction="BUY", stop_pips=10.0, take_profit_pips=22.0,
                  risk_reward=2.2, confidence=0.7, reason_code="TECH_CONFIRMED_BUY", timestamp_utc=t.isoformat(),
                  be_trigger_r=0.0, partial_close_r=0.0, trailing_atr_mult=0.0, tp_mode="FIXED")
    regime = dict(regime="TRENDING_BULL", trend_state="BULLISH", volatility_state="NORMAL", confidence=0.8,
                  reason_code="REGIME_TREND_BULL", timestamp_utc=t.isoformat(), atr_ratio=1.0)
    return Candidate(t, "EURUSD", signal, regime)


def _simulated(gap_target: bool = False):
    """One FIXED-target BUY from simulate; the target is hit intrabar unless gap_target."""
    prices = np.full(400, 1.1000)
    prices[301:] = 1.1030 if gap_target else 1.1015
    m15 = _frame("2026-01-05 07:00", 400, "15min", prices)
    m15.loc[m15.index[302], "high"] = 1.1060
    frames = {"EURUSD": {"M15": m15}}
    cand = _candidate(m15.index[299] + pd.Timedelta(minutes=15))
    result = simulate([cand], frames, POLICY, COST, SimulationConfig())
    assert len(result.trades) == 1 and result.trades[0]["exit_reason"] == "TARGET"
    return frames, [cand], result.trades[0]


def _fill_checks(trades, cands, frames):
    return {c["id"]: c for c in (gate.check_same_bar_fill(trades, cands, frames, COST),
                                 gate.check_fills_within_bid_ask(trades, frames, COST),
                                 gate.check_no_fill_in_gap_or_closed_market(trades, frames))}


def _stub(check_id: str, status: str = "PASS") -> dict:
    return {"id": check_id, "status": status, "blocking": check_id not in gate.NON_BLOCKING, "detail": []}


def test_verdict_keeps_check_order():
    checks = [_stub(i) for i in reversed(gate.CHECK_ORDER)]
    assert [c["id"] for c in gate.build_verdict(checks)["checks"]] == list(gate.CHECK_ORDER)


def test_pass_is_evidence_eligible_even_with_nonblocking_warning():
    checks = [_stub(i, "INCONCLUSIVE" if i == "R1C_WARMUP_DEVIATION" else "PASS") for i in gate.CHECK_ORDER]
    verdict = gate.build_verdict(checks, context={"policy_hash": "abc"})
    assert verdict["verdict"] == "PASS" and verdict["evidence_status"] == "EVIDENCE_ELIGIBLE"
    assert verdict["warnings"] == ["R1C_WARMUP_DEVIATION: INCONCLUSIVE"]
    assert gate.render_gate_markdown(verdict).splitlines()[0] == "# Replay authenticity gate: PASS"


def test_blocking_failure_is_not_evidence():
    checks = [_stub(i, "FAIL" if i == "R2_FILLS_WITHIN_BID_ASK" else "PASS") for i in gate.CHECK_ORDER]
    verdict = gate.build_verdict(checks)
    assert verdict["verdict"] == "FAIL" and verdict["evidence_status"] == "NOT_EVIDENCE"
    assert any(r.startswith("R2_FILLS_WITHIN_BID_ASK") for r in verdict["reasons"])
    assert gate.render_gate_markdown(verdict).startswith("# NOT EVIDENCE")


def test_simulated_trade_passes_all_fill_checks():
    frames, cands, trade = _simulated()
    checks = _fill_checks([trade], cands, frames)
    assert {k: c["status"] for k, c in checks.items()} == {
        "R2_NO_SAME_BAR_FILL": "PASS", "R2_FILLS_WITHIN_BID_ASK": "PASS", "R2_NO_FILL_IN_GAP_OR_CLOSED_MARKET": "PASS"}


def test_same_bar_fill_is_detected():
    frames, cands, trade = _simulated()
    m15 = frames["EURUSD"]["M15"]
    entry = pd.Timestamp(trade["entry_time"])
    j = m15.index.get_loc(entry)
    m15.loc[entry, "open"] += 0.0003  # entry bar opens away from the previous close
    spread = COST.spread_pips("EURUSD", 0) * 0.0001
    leaked = dict(trade, entry_price=float(m15["close"].iat[j - 1]) + spread)
    assert gate.check_same_bar_fill([leaked], cands, frames, COST)["status"] == "FAIL"


def test_exit_above_bid_high_is_detected():
    frames, cands, trade = _simulated()
    m15 = frames["EURUSD"]["M15"]
    exit_bar = pd.Timestamp(trade["exit_time"]) - pd.Timedelta(minutes=15)
    bad = dict(trade, exit_price=float(m15.loc[exit_bar, "high"]) + 0.0010)
    assert gate.check_fills_within_bid_ask([bad], frames, COST)["status"] == "FAIL"


def test_gap_through_target_fill_is_reported_not_excused():
    # protective_exit fills a gapped target at the target, which lies below the bar's bid low.
    frames, cands, trade = _simulated(gap_target=True)
    result = gate.check_fills_within_bid_ask([trade], frames, COST)
    assert result["status"] == "FAIL" and result["detail"][0]["fill"] == "TARGET"


def test_weekend_fill_fails_closed_market_check():
    m15 = _frame("2026-01-10 10:00", 20, "15min", np.full(20, 1.1))  # Saturday
    frames = {"EURUSD": {"M15": m15}}
    trade = dict(trade_id="W", symbol="EURUSD", direction="BUY", entry_time="2026-01-10T12:00:00+00:00",
                 exit_time="2026-01-10T12:15:00+00:00")
    result = gate.check_no_fill_in_gap_or_closed_market([trade], frames)
    assert result["status"] == "FAIL"
    assert result["detail"][0]["reason"] == "bar inside the FX weekly close"
    assert gate.is_fx_market_closed(pd.Timestamp("2026-01-10T12:00:00Z"))
    assert not gate.is_fx_market_closed(pd.Timestamp("2026-01-07T12:00:00Z"))


def test_exit_without_bar_fails_gap_check():
    frames, cands, trade = _simulated()
    end = frames["EURUSD"]["M15"].index[-1]
    missing = dict(trade, exit_time=(end + pd.Timedelta(hours=3)).isoformat())
    result = gate.check_no_fill_in_gap_or_closed_market([missing], frames)
    assert result["status"] == "FAIL" and result["detail"][0]["reason"] == "no M15 bar for the fill"


def test_fixture_end_to_end_is_not_evidence():
    frames = load_frames(bridge_dir=FIXTURE)
    policy = policy_for_variant("baseline")[0]
    cost = COST_SCENARIOS["base"]
    candidates, _, result = run_replay_in_process(frames, policy, cost)
    verdict = gate.audit_replay(frames=frames, policy=policy, cost=cost, candidates=candidates, result=result,
                                max_decisions=8)
    status = {c["id"]: c["status"] for c in verdict["checks"]}
    assert verdict["verdict"] == "FAIL" and verdict["evidence_status"] == "NOT_EVIDENCE"
    for check_id in ("R1A_TRUNCATION", "R1B_FUTURE_CORRUPTION", "R1C_WARMUP_PARITY"):
        assert status[check_id] == "PASS"
    # Observed on the fixture: every fill passes the literal checks.
    for check_id in ("R2_NO_SAME_BAR_FILL", "R2_FILLS_WITHIN_BID_ASK", "R2_NO_FILL_IN_GAP_OR_CLOSED_MARKET"):
        assert status[check_id] == "PASS"
    assert status["R4_COST_STRESS"] == "FAIL"  # mean r < 0 at base cost
    assert verdict["reasons"] == ["R4_COST_STRESS: FAIL"]
