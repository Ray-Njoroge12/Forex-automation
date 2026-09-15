from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from application.routing_service import ReservationRejected, RoutingService
from backtesting.event_replay import MultiSymbolEventReplay
from backtesting.dataset_manifest import inspect_ohlc_csv
from bridge.signal_router import SignalRouter
from config_microcapital import get_policy_config
from core.account_status import AccountStatus
from core.data_quality.freshness import evaluate_calendar_freshness, evaluate_rate_differential_freshness
from core.policy.envelope import attach_execution_envelope, validate_execution_envelope
from database import db as db_mod
from validation import update_market_context
from validation.prepare_replay_dataset import write_manifest
from validation.readiness_preflight import run_preflight
from validation.print_policy_hash import build_policy_pin
from validation.ea_demo_matrix import build_demo_matrix, write_demo_matrix
from backtesting.experiment_guard import assess_optimization_readiness


@contextmanager
def _temp_conn(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _patch_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "get_conn", lambda db_path=path: _temp_conn(db_path))
    db_mod.initialize_schema()
    db_mod.migrate_add_trade_reservations()
    return path


def _payload(trade_id: str, symbol: str, risk: float = 0.02) -> dict[str, object]:
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "direction": "BUY",
        "risk_percent": risk,
        "stop_pips": 10.0,
        "take_profit_pips": 22.0,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def test_same_cycle_reservations_enforce_position_and_exposure_caps(tmp_path, monkeypatch) -> None:
    _patch_database(tmp_path, monkeypatch)
    policy = get_policy_config(mode_id="core_srs")
    router = SignalRouter(tmp_path / "pending", tmp_path / "locks")
    service = RoutingService(router, policy)
    account = AccountStatus(balance=10_000.0, equity=10_000.0)

    service.reserve_and_route(_payload("T1", "EURUSD"), account_status=account)
    service.reserve_and_route(_payload("T2", "GBPUSD"), account_status=account)
    rejected = []
    for trade_id, symbol in (("T3", "AUDUSD"), ("T4", "USDCAD"), ("T5", "USDJPY"), ("T6", "USDCHF")):
        with pytest.raises(ReservationRejected, match="RESERVATION_MAX_POSITIONS"):
            service.reserve_and_route(_payload(trade_id, symbol, 0.005), account_status=account)
        rejected.append(trade_id)

    files = sorted((tmp_path / "pending").glob("*.json"))
    assert [path.stem for path in files] == ["T1", "T2"]
    assert rejected == ["T3", "T4", "T5", "T6"]


def test_reservation_rejects_projected_exposure_before_position_cap(tmp_path, monkeypatch) -> None:
    _patch_database(tmp_path, monkeypatch)
    policy = get_policy_config(mode_id="core_srs")
    service = RoutingService(SignalRouter(tmp_path / "pending", tmp_path / "locks"), policy)
    account = AccountStatus(balance=10_000.0, equity=10_000.0)
    service.reserve_and_route(_payload("T1", "EURUSD", 0.032), account_status=account)
    with pytest.raises(ReservationRejected, match="RESERVATION_MAX_EXPOSURE"):
        service.reserve_and_route(_payload("T2", "GBPUSD", 0.02), account_status=account)


def test_expired_execution_envelope_is_rejected() -> None:
    policy = get_policy_config(mode_id="core_srs")
    issued = datetime.now(timezone.utc) - timedelta(seconds=10)
    payload = attach_execution_envelope(
        _payload("T1", "EURUSD"),
        policy=policy,
        reservation_id="RSV_1",
        now_utc=issued,
        ttl_seconds=5,
    )
    with pytest.raises(ValueError, match="expired"):
        validate_execution_envelope(payload, now_utc=datetime.now(timezone.utc))


def test_core_policy_and_ea_use_stricter_two_pip_spread_ceiling() -> None:
    policy = get_policy_config(mode_id="core_srs")
    assert policy["MAX_SPREAD_PIPS"] == 2.0
    ea_source = (Path(__file__).parents[1] / "mt5_ea" / "FX_Execution.mq5").read_text(encoding="utf-8")
    assert "MathMin(MaxSpreadPips, signalMaxSpreadPips)" in ea_source
    assert 'ExtractJsonDouble(content, "max_spread_pips"' in ea_source


def test_calendar_freshness_fails_closed_when_coverage_is_stale() -> None:
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    decision = evaluate_calendar_freshness(
        [{"datetime_utc": "2026-06-18T12:00:00Z"}],
        now_utc=now,
    )
    assert not decision.approved
    assert decision.reason_code == "CALENDAR_STALE"


def test_rate_differential_freshness_disables_stale_soft_input() -> None:
    decision = evaluate_rate_differential_freshness(
        {"_last_updated": "2026-02-28"},
        now_utc=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )
    assert not decision.approved
    assert decision.reason_code == "RATE_DIFFERENTIAL_STALE"


def test_multi_symbol_replay_is_chronological_and_never_exposes_future_rows() -> None:
    times = pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"])
    frame = pd.DataFrame({"close": [1.0, 2.0]}, index=times)
    events = list(MultiSymbolEventReplay({"EURUSD": frame, "GBPUSD": frame}).events())
    assert [(event.timestamp, event.symbol) for event in events] == [
        (times[0], "EURUSD"),
        (times[0], "GBPUSD"),
        (times[1], "EURUSD"),
        (times[1], "GBPUSD"),
    ]
    assert all(event.closed_bars.index.max() <= event.timestamp for event in events)


def test_trade_proposal_upsert_preserves_row_identity_and_appends_lifecycle(tmp_path, monkeypatch) -> None:
    _patch_database(tmp_path, monkeypatch)
    from core.types import TechnicalSignal

    signal = TechnicalSignal(
        trade_id="T_UPSERT",
        symbol="EURUSD",
        direction="BUY",
        stop_pips=10.0,
        take_profit_pips=22.0,
        risk_reward=2.2,
        confidence=0.7,
        reason_code="TEST",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    db_mod.insert_trade_proposal(signal, "PENDING", "FIRST", 0.02, "TRENDING_BULL")
    with db_mod.get_conn() as conn:
        first_id = int(conn.execute("SELECT id FROM trades WHERE trade_id='T_UPSERT'").fetchone()["id"])
    db_mod.insert_trade_proposal(signal, "REJECTED", "SECOND", 0.0, "TRENDING_BULL")
    with db_mod.get_conn() as conn:
        row = conn.execute("SELECT id, status FROM trades WHERE trade_id='T_UPSERT'").fetchone()
        events = conn.execute("SELECT COUNT(*) AS n FROM trade_lifecycle_events WHERE trade_id='T_UPSERT'").fetchone()
    assert int(row["id"]) == first_id
    assert row["status"] == "REJECTED"
    assert int(events["n"]) == 2


def test_market_context_import_refuses_stale_calendar(tmp_path) -> None:
    source = tmp_path / "calendar.json"
    source.write_text(
        '[{"datetime_utc":"2026-06-18T12:00:00Z","currency":"USD","impact":"high","event":"CPI"}]',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="CALENDAR_STALE"):
        update_market_context.install_calendar(
            source,
            now_utc=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )


def test_initializer_runs_current_migrations_at_requested_database(tmp_path) -> None:
    from init_system import initialize_database

    target = tmp_path / "nested" / "trading_state.db"
    initialize_database(target)
    with sqlite3.connect(target) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trades", "trade_reservations", "trade_lifecycle_events"} <= tables


def test_replay_dataset_manifest_is_hashed_and_rejects_invalid_candles(tmp_path) -> None:
    source = tmp_path / "EURUSD.csv"
    source.write_text(
        "time,open,high,low,close\n"
        "2026-01-01T00:00:00Z,1.1000,1.1010,1.0990,1.1005\n"
        "2026-01-01T00:15:00Z,1.1005,1.1020,1.1000,1.1015\n",
        encoding="utf-8",
    )
    manifest = inspect_ohlc_csv(source, symbol="eurusd")
    assert manifest.symbol == "EURUSD"
    assert manifest.rows == 2
    assert len(manifest.sha256) == 64
    output = write_manifest(csv_path=source, symbol="EURUSD", output=tmp_path / "manifest.json")
    assert output.is_file()

    invalid = tmp_path / "invalid.csv"
    invalid.write_text(
        "time,open,high,low,close\n2026-01-01T00:00:00Z,1.1,1.0,1.2,1.1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="high below"):
        inspect_ohlc_csv(invalid, symbol="EURUSD")


def test_readiness_preflight_reports_stale_market_context_and_local_guards(tmp_path) -> None:
    root = tmp_path
    (root / "data").mkdir()
    (root / "database").mkdir()
    (root / "mt5_ea").mkdir()
    (root / "data" / "economic_calendar.json").write_text(
        '[{"datetime_utc":"2026-06-18T12:00:00Z","currency":"USD","impact":"high","event":"CPI"}]',
        encoding="utf-8",
    )
    (root / "data" / "rate_differentials.json").write_text(
        '{"_last_updated":"2026-02-28"}', encoding="utf-8"
    )
    (root / "mt5_ea" / "FX_Execution.mq5").write_text(
        'ExtractJsonDouble(content, "max_spread_pips"\n'
        'MathMin(MaxSpreadPips, signalMaxSpreadPips)\n'
        'ExtractJsonString(content, "reservation_id"\n'
        'input bool RequirePolicyHashPin = true\n'
        'REJECTED_POLICY_CONFIGURATION\n',
        encoding="utf-8",
    )
    with sqlite3.connect(root / "database" / "trading_state.db") as conn:
        for table in ("trades", "trade_reservations", "trade_lifecycle_events"):
            conn.execute(f"CREATE TABLE {table} (id INTEGER)")
    results = {result.check_id: result for result in run_preflight(root)}
    assert not results["calendar_freshness"].approved
    assert not results["rate_differential_freshness"].approved
    assert results["database_schema"].approved
    assert results["ea_source_guard"].approved


def test_sessions_follow_london_and_new_york_dst_rules() -> None:
    from core.filters.session_filter import get_active_session

    assert get_active_session(datetime(2026, 1, 5, 7, tzinfo=timezone.utc)) == "london"
    assert get_active_session(datetime(2026, 7, 6, 6, tzinfo=timezone.utc)) == "london"
    assert get_active_session(datetime(2026, 1, 5, 16, tzinfo=timezone.utc)) == "newyork"
    assert get_active_session(datetime(2026, 7, 6, 15, tzinfo=timezone.utc)) == "newyork"
    with pytest.raises(ValueError, match="timezone-aware"):
        get_active_session(datetime(2026, 1, 5, 7))


def test_policy_pin_matches_core_policy_and_ea_requires_it() -> None:
    pin = build_policy_pin("core_srs")
    assert pin["mode_id"] == "core_srs"
    assert len(pin["policy_hash"]) == 64
    ea_source = (Path(__file__).parents[1] / "mt5_ea" / "FX_Execution.mq5").read_text(encoding="utf-8")
    assert "input bool RequirePolicyHashPin = true" in ea_source
    assert "REJECTED_POLICY_CONFIGURATION" in ea_source


def test_portfolio_correlation_uses_aligned_returns_with_minimum_history() -> None:
    from core.agents.portfolio_manager import PortfolioManager

    index = pd.date_range("2026-01-01", periods=61, freq="D", tz="UTC")
    close = pd.Series(range(100, 161), index=index, dtype=float)
    frame = pd.DataFrame({"close": close})
    manager = PortfolioManager(fetch_ohlc=lambda symbol, timeframe, count: frame)
    assert manager._compute_pair_correlation("EURUSD", "GBPUSD") == 1.0

    short = pd.DataFrame({"close": close.iloc[:20]})
    short_manager = PortfolioManager(fetch_ohlc=lambda symbol, timeframe, count: short)
    assert short_manager._compute_pair_correlation("EURUSD", "GBPUSD") is None


def test_ea_demo_matrix_is_policy_pinned_and_records_expected_cases(tmp_path) -> None:
    pin, cases = build_demo_matrix("core_srs")
    assert len(pin["policy_hash"]) == 64
    assert {case.case_id for case in cases} == {
        "valid", "expired", "policy_hash_mismatch", "spread_limit", "malformed_schema", "position_limit"
    }
    assert next(case for case in cases if case.case_id == "policy_hash_mismatch").payload["policy_hash"] == "0" * 64
    manifest = write_demo_matrix(tmp_path / "matrix")
    assert manifest.is_file()
    assert (manifest.parent / "valid.json").is_file()


def test_calendar_supports_conservative_all_day_blackout() -> None:
    from core.filters.calendar_filter import is_news_blackout

    events = [{
        "datetime_utc": "2026-09-17T00:00:00Z",
        "currency": "GBP",
        "impact": "high",
        "event": "Policy decision",
        "blackout_before_minutes": 0,
        "blackout_after_minutes": 1439,
    }]
    assert is_news_blackout("GBPUSD", datetime(2026, 9, 17, 23, 58, tzinfo=timezone.utc), events)


def test_optimization_guard_requires_matching_long_realistic_dataset(tmp_path) -> None:
    source = tmp_path / "EURUSD.csv"
    dates = pd.date_range("2025-01-01", periods=401, freq="D", tz="UTC")
    rows = "time,open,high,low,close\n" + "\n".join(
        f"{stamp.isoformat()},{1.1 + i * 0.0001:.4f},{1.101 + i * 0.0001:.4f},{1.099 + i * 0.0001:.4f},{1.1005 + i * 0.0001:.4f}"
        for i, stamp in enumerate(dates)
    )
    source.write_text(rows + "\n", encoding="utf-8")
    manifest_path = write_manifest(csv_path=source, symbol="EURUSD", output=tmp_path / "manifest.json")
    approved = assess_optimization_readiness(
        csv_path=source, manifest_path=manifest_path, symbol="EURUSD", mode_id="preserve_10"
    )
    assert approved.approved
    smoke = assess_optimization_readiness(
        csv_path=source, manifest_path=manifest_path, symbol="EURUSD", mode_id="core_srs"
    )
    assert not smoke.approved
    assert "OPTIMIZATION_REQUIRES_REALISTIC_PROFILE" in smoke.reason_codes
