"""The engine persists through the injected repository (August audit §2).

Application services must not import ``database``; ``main.Engine`` hands its
repository to every service. With ``InMemoryEngineRepository`` the golden
decision cycle (``tests/test_contract_engine.py``) gives the same record and a
fill and exit are processed without touching SQLite.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from database.repositories.memory import InMemoryEngineRepository
from database.repositories.sqlite import SqliteEngineRepository
from tests.engine_harness import make_file_bridge_engine
from tests.test_contract_engine import (
    GOLDEN,
    NOW_FIXED,
    _clock_modules,
    _freeze_engine_clock,
    _golden_bridge,
    _synthetic_calendar,
)
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)

APPLICATION_DIR = Path(__file__).resolve().parents[1] / "application"
SQLITE_TABLES = ("trades", "trade_reservations", "decision_funnel_events", "risk_events", "account_metrics",
                 "feedback_receipts", "trade_lifecycle_events", "engine_runtime_state")


def test_application_services_do_not_import_the_database_package():
    offenders = []
    for path in sorted(APPLICATION_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            offenders += [f"{path.name}:{module}" for module in modules
                          if module == "database" or module.startswith("database.")]
    assert offenders == []


def test_main_composes_the_engine_with_the_sqlite_repository(tmp_path, monkeypatch):
    import main
    from core import alerts

    captured: dict = {}

    class _RecordingEngine:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def run(self, mode, iterations=0):
            captured["ran"] = (mode, iterations)

    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setenv("BRIDGE_BASE_PATH", str(tmp_path))
    for name in ("configure_logging", "run_all_migrations", "start_health_api"):
        monkeypatch.setattr(main, name, lambda *a, **k: None)
    monkeypatch.setattr(alerts, "send_telegram_message", lambda *a, **k: None)
    monkeypatch.setattr(main, "Engine", _RecordingEngine)

    assert main.main(["--mode", "smoke"]) == 0
    assert isinstance(captured["repository"], SqliteEngineRepository)
    assert captured["ran"] == ("smoke", 0)


def test_engine_with_in_memory_repository_reproduces_golden_cycle_and_feedback(tmp_path, monkeypatch, isolated_db):
    bridge_dir = _golden_bridge(tmp_path / "bridge")
    _freeze_engine_clock(monkeypatch)
    for module in _clock_modules():
        for name in ("alert_risk_halt", "alert_trade_execution", "alert_trade_exit"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, lambda *a, **k: None)
    repository = InMemoryEngineRepository()
    engine = make_file_bridge_engine(bridge_dir, monkeypatch, clock=lambda: NOW_FIXED, repository=repository)
    assert engine.repository is repository
    assert engine.routing_service.repository is repository
    engine._calendar_events = _synthetic_calendar()
    engine.ranker_loaded = False

    engine._update_account_state()
    engine._decision_cycle()

    record = {
        "funnel": [[r["stage"], r["outcome"], r["reason_code"], r["symbol"]] for r in repository.decision_funnel_events],
        "risk_events": [r["rule_name"] for r in repository.risk_events],
    }
    assert record == json.loads(GOLDEN.read_text(encoding="utf-8"))
    [pending] = list((bridge_dir / "pending_signals").glob("*.json"))
    trade_id = pending.stem
    assert [(r["trade_id"], r["symbol"], r["state"]) for r in repository.trade_reservations] == [
        (trade_id, "GBPUSD", "ROUTED")]

    # The EA's fill, then its final exit, as the file bridge delivers them.
    (bridge_dir / "feedback" / f"execution_{trade_id}.json").write_text(json.dumps({
        "trade_id": trade_id, "ticket": 777001, "position_ticket": 777001, "status": "EXECUTED",
        "entry_price": 1.34175, "lot_size": 23.53, "slippage": 0.0, "spread_at_entry": 0.0,
        "profit_loss": 0.0, "r_multiple": 0.0, "close_time": "2026-11-03T16:30:05Z",
    }))
    engine._consume_feedback()
    [opened] = repository.list_open_trades(evidence_stream=engine.evidence_context.evidence_stream,
                                           account_scope=engine.evidence_context.account_scope)
    assert (opened["trade_id"], opened["status"], opened["position_ticket"]) == (trade_id, "EXECUTED_OPEN", 777001)

    (bridge_dir / "exits" / "exit_777001.json").write_text(json.dumps({
        "ticket": 777001, "position_ticket": 777001, "trade_id": trade_id, "profit_loss": 1576.51,
        "status": "CLOSED_WIN", "is_final_exit": True, "r_multiple": 0.4926,
        "close_time": "2026-11-03T19:03:16Z", "close_time_server": "2026-11-03T21:03:16",
    }))
    engine._consume_feedback()
    assert repository.list_open_trades() == []
    assert repository.get_trade_by_identifiers(trade_id=trade_id)["status"] == "CLOSED_WIN"
    assert repository.trade_reservations[0]["state"] == "CLOSED"
    assert [(r["feedback_kind"], r["trade_id"]) for r in repository.feedback_receipts] == [
        ("execution", trade_id), ("exit", trade_id)]
    assert repository.account_metrics[-1]["consecutive_losses"] == 0
    assert list((bridge_dir / "feedback").glob("execution_*.json")) == []
    assert list((bridge_dir / "exits").glob("exit_*.json")) == []

    # Nothing reached SQLite. (SignalRouter's duplicate check in bridge/ still reads it.)
    with isolated_db() as conn:
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in SQLITE_TABLES}
    assert counts == {table: 0 for table in SQLITE_TABLES}
