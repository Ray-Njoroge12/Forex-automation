"""research.common: data discovery, in-process replay determinism, fingerprints, and the no-orders guard."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pandas as pd

from backtesting.unified_replay import COST_SCENARIOS, Candidate
from research.common import (
    decision_fingerprint, discover_symbols, load_frames, policy_for_variant, run_replay_in_process,
)

FIXTURE = Path(__file__).parent / "fixtures" / "live_bars_2026-11-03"
RESEARCH_DIR = Path(__file__).parents[1] / "research"


def test_discovers_fixture_symbols_in_srs_order():
    assert discover_symbols(bridge_dir=FIXTURE) == ["GBPUSD", "AUDUSD"]


def test_in_process_replay_is_deterministic_on_fixture():
    frames = load_frames(bridge_dir=FIXTURE)
    policy, _ = policy_for_variant("baseline")
    _, _, first = run_replay_in_process(frames, policy, COST_SCENARIOS["base"])
    _, _, second = run_replay_in_process(frames, policy, COST_SCENARIOS["base"])
    assert len(first.trades) == 19
    assert [t["r_multiple"] for t in first.trades] == [t["r_multiple"] for t in second.trades]


def test_fingerprint_drops_volatile_fields():
    t = pd.Timestamp("2026-11-03T16:30:00Z")
    cand = Candidate(t, "GBPUSD", {"trade_id": "abc", "timestamp_utc": "now", "direction": "BUY", "stop_pips": 13.6},
                     {"regime": "TRENDING_BULL", "timestamp_utc": "now"})
    fp = decision_fingerprint(cand, Counter({"CANDIDATE": 1}))
    assert fp == {"funnel": "CANDIDATE", "signal": {"direction": "BUY", "stop_pips": 13.6},
                  "regime": {"regime": "TRENDING_BULL"}}


def test_research_modules_cannot_route_orders():
    forbidden = ["signal_router", "pending_signals", "MetaTrader5", "order_send", '"--mode", "demo"']
    bridge_import = re.compile(r"^\s*(from|import)\s+bridge\b", re.MULTILINE)
    sources = sorted(RESEARCH_DIR.glob("*.py"))
    assert sources
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, f"{path.name} mentions {word}"
        assert not bridge_import.search(text), f"{path.name} imports the bridge package"
