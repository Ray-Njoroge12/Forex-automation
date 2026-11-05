"""Source guards for the EA ATR handle cache (host tolerance backlog item 4, EA 2026.11.05-1).

ManageOpenPositions runs on every timer tick. GetATR used to create and release
an iATR handle on each call; it now reuses one cached handle per
symbol/timeframe/period and OnDeinit releases them. Order placement and the
envelope checks are covered by test_ea_cent_guards.py and must not change.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_ea_cent_guards import _function_body

EA_SOURCE = Path(__file__).parents[1] / "mt5_ea" / "FX_Execution.mq5"


@pytest.fixture(scope="module")
def source() -> str:
    return EA_SOURCE.read_text(encoding="utf-8")


def test_get_atr_uses_the_cache_and_never_releases_per_call(source):
    body = _function_body(source, "GetATR")
    assert "CachedAtrHandle(sym, tf, period)" in body
    assert "iATR(" not in body
    assert "IndicatorRelease" not in body


def test_cache_creates_a_handle_only_on_a_miss_keyed_by_symbol_timeframe_and_period(source):
    body = _function_body(source, "CachedAtrHandle")
    lookup = body.find("return g_atr_handles[i]")
    create = body.find("iATR(sym, tf, period)")
    assert 0 <= lookup < create
    assert "g_atr_symbols[i] == sym && g_atr_tfs[i] == tf && g_atr_periods[i] == period" in body
    assert "if(handle == INVALID_HANDLE) return INVALID_HANDLE;" in body
    assert body.count("iATR(") == 1


def test_cache_is_bounded_and_released_in_deinit(source):
    assert re.search(r"#define ATR_CACHE_SIZE \d+", source)
    assert "g_atr_count >= ATR_CACHE_SIZE" in _function_body(source, "CachedAtrHandle")
    release = _function_body(source, "ReleaseAtrHandles")
    assert "IndicatorRelease(g_atr_handles[i])" in release and "g_atr_count = 0;" in release
    deinit = _function_body(source, "OnDeinit")
    assert "EventKillTimer();" in deinit and "ReleaseAtrHandles();" in deinit


def test_iatr_and_indicator_release_appear_only_in_the_cache(source):
    assert source.count("iATR(") == 1
    assert source.count("IndicatorRelease(") == 1


def test_manage_open_positions_still_reads_atr_through_get_atr(source):
    body = _function_body(source, "ManageOpenPositions")
    assert "GetATR(sym, PERIOD_M15, TrailingATRPeriod)" in body
    assert "iATR(" not in body and "IndicatorRelease" not in body
