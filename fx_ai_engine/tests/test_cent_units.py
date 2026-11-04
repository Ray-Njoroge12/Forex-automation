"""C2: every Python-visible money value is US dollars after the bridge boundary.

A cent account (currency USC) reports balance, equity, margin, profit and tick
value in cents. The bridge layer converts them once (``account_unit_scale``);
lots are never converted. USD output must stay identical to the raw file values.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.mt5_bridge as mt5_bridge
from bridge.execution_feedback import ExecutionFeedbackReader
from core.file_bridge_connection import FileBridgeConnection
from core.mt5_bridge import MT5Connection, account_unit_scale, usd_from_account_units
from tests.bridge_fixtures import build_full_usd_bridge, convert_bridge_to_usc
from tests.engine_harness import make_file_bridge_engine
from tests.test_engine_file_bridge import isolated_db  # noqa: F401  (fixture)
from tests.test_file_bridge_connection import build_bridge
from tests.test_legacy_exit_consumption import FIXTURE, LIVE_DEALS, TRADE_ID, _seed_trade, _trade


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _usd_bridge(tmp_path: Path) -> Path:
    return build_full_usd_bridge(tmp_path / "usd", _now(), bar_count=5)


def _usc_bridge(tmp_path: Path, balance_usd: float | None = 10.0) -> Path:
    return convert_bridge_to_usc(_usd_bridge(tmp_path), tmp_path / "usc", balance_usd=balance_usd)


def _connect(path: Path) -> FileBridgeConnection:
    conn = FileBridgeConnection(path)
    assert conn.connect(retry_count=1), conn.last_error
    return conn


def _set_currency(path: Path, currency: str) -> None:
    snapshot_path = path / "feedback" / "account_snapshot.json"
    payload = json.loads(snapshot_path.read_text())
    payload["currency"] = currency
    snapshot_path.write_text(json.dumps(payload))


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def test_unit_helpers():
    assert account_unit_scale("USD") == 1.0
    assert account_unit_scale("usc") == 0.01
    assert account_unit_scale("USD-Cent") == 0.01
    assert account_unit_scale("EUR") == 1.0
    assert account_unit_scale(None) == 1.0
    assert usd_from_account_units(123.456, 1.0) == 123.456
    assert usd_from_account_units(157651, 0.01) == pytest.approx(1576.51)


# ----------------------------------------------------------------------
# File bridge
# ----------------------------------------------------------------------
def test_usc_fixture_snapshot_is_usd(tmp_path):
    conn = _connect(_usc_bridge(tmp_path))
    raw = json.loads((tmp_path / "usc" / "feedback" / "account_snapshot.json").read_text())
    assert raw["currency"] == "USC"
    assert raw["balance"] == pytest.approx(1000.0)
    snapshot = conn.get_account_snapshot()
    assert "error" not in snapshot
    assert snapshot["balance"] == pytest.approx(10.0)
    assert snapshot["equity"] == pytest.approx(10.0)
    assert snapshot["margin_free"] == pytest.approx(10.0)
    assert snapshot["floating_pnl"] == pytest.approx(raw["floating_pnl"] * 0.01)
    assert snapshot["money_unit"] == "USD"
    assert snapshot["unit_scale"] == 0.01
    # The cached raw payload is never mutated.
    again = conn.get_account_snapshot()
    assert again["balance"] == pytest.approx(10.0)
    assert conn._account_snapshot_payload()["balance"] == pytest.approx(1000.0)


def test_usd_snapshot_is_byte_identical(tmp_path):
    bridge = _usd_bridge(tmp_path)
    conn = _connect(bridge)
    raw = json.loads((bridge / "feedback" / "account_snapshot.json").read_text())
    snapshot = conn.get_account_snapshot()
    assert json.dumps(snapshot, sort_keys=True) == json.dumps(raw, sort_keys=True)
    assert "money_unit" not in snapshot and "unit_scale" not in snapshot
    contract = conn.get_symbol_execution_contract("EURUSD")
    assert contract.tick_value == 1.0
    summary = conn.get_position_history_summary(999)
    assert summary["profit_loss"] == pytest.approx(49.3)
    assert conn.get_trade_history_summary("AI_TEST_1")["profit_loss"] == pytest.approx(49.3)


def test_usc_contract_tick_value_usd(tmp_path):
    conn = _connect(_usc_bridge(tmp_path))
    contract = conn.get_symbol_execution_contract("EURUSD")
    assert contract.tick_value == pytest.approx(0.01)
    assert contract.min_lot == 0.01  # lots are never converted


def test_25_pip_1_5_percent_is_feasible_on_usc(tmp_path):
    conn = _connect(_usc_bridge(tmp_path, balance_usd=10.0))
    decision = conn.evaluate_trade_feasibility("EURUSD", 0.015, 25.0)
    assert decision.approved, decision.details
    assert decision.reason_code == "TRADE_FEASIBLE"
    assert decision.estimated_lot == pytest.approx(0.06)


def test_25_pip_1_5_percent_is_rejected_on_the_usd_twin_of_ten_dollars(tmp_path):
    """The same bridge in USD would need a lot below the broker minimum; USC does not change that arithmetic."""
    bridge = _usd_bridge(tmp_path)
    snapshot_path = bridge / "feedback" / "account_snapshot.json"
    payload = json.loads(snapshot_path.read_text())
    payload.update(balance=10.0, equity=10.0, margin_free=10.0)
    snapshot_path.write_text(json.dumps(payload))
    decision = _connect(bridge).evaluate_trade_feasibility("EURUSD", 0.015, 25.0)
    assert not decision.approved
    assert decision.reason_code == "REJECTED_LOT_PREROUTE"


def test_fixed_risk_eligibility_on_usc(tmp_path):
    conn = _connect(_usc_bridge(tmp_path, balance_usd=10.0))
    decision = conn.evaluate_fixed_risk_eligibility("EURUSD", 0.15, 25.0)
    assert decision.can_assess
    assert decision.approved, decision.details
    # 250 points x USD 0.01 per point per lot x 0.01 lot. The USD account's minimum lot costs 100x this.
    assert decision.minimum_risk_usd == pytest.approx(0.025)
    usd = _connect(_usd_bridge(tmp_path / "twin")).evaluate_fixed_risk_eligibility("EURUSD", 0.15, 25.0)
    assert usd.minimum_risk_usd == pytest.approx(2.5)
    assert not usd.approved


def test_deal_profit_cents_to_dollars_file_bridge(tmp_path):
    usd = _connect(_usd_bridge(tmp_path))
    usc = _connect(_usc_bridge(tmp_path))
    for conn in (usd, usc):
        assert conn.get_position_history_summary(999)["profit_loss"] == pytest.approx(49.3)
        assert conn.get_trade_history_summary("AI_TEST_1")["profit_loss"] == pytest.approx(49.3)
    legs = usc.get_position_history_summary(999)["close_legs"]
    assert [leg["profit_loss"] for leg in legs] == [pytest.approx(49.3)]
    assert legs[0]["volume"] == pytest.approx(10.0)  # deal volume is passed through, not converted


def test_preserve_10_file_bridge_remains_unsupported(tmp_path):
    for conn in (_connect(_usd_bridge(tmp_path / "a")), _connect(_usc_bridge(tmp_path / "b"))):
        facts = conn.get_preserve_10_approval_facts()
        assert not facts.can_assess
        assert facts.reason_code == "FILE_BRIDGE_PRESERVE_10_UNSUPPORTED"


def test_unknown_currency_is_not_converted(tmp_path):
    bridge = _usd_bridge(tmp_path)
    _set_currency(bridge, "EUR")
    conn = _connect(bridge)
    raw = json.loads((bridge / "feedback" / "account_snapshot.json").read_text())
    snapshot = conn.get_account_snapshot()
    assert json.dumps(snapshot, sort_keys=True) == json.dumps(raw, sort_keys=True)
    assert conn.get_symbol_execution_contract("EURUSD").tick_value == 1.0
    assert conn.get_position_history_summary(999)["profit_loss"] == pytest.approx(49.3)
    assert conn.last_error is None


def test_currency_falls_back_to_cached_value_when_files_vanish(tmp_path):
    bridge = _usc_bridge(tmp_path)
    conn = _connect(bridge)
    assert conn.get_account_snapshot()["balance"] == pytest.approx(10.0)
    (bridge / "feedback" / "account_snapshot.json").unlink()
    assert conn._unit_scale() == 0.01


# ----------------------------------------------------------------------
# MT5Connection (mock profile preserve_10_ready: USC account, balance 1000)
# ----------------------------------------------------------------------
@pytest.fixture
def usc_mock(monkeypatch):
    monkeypatch.setenv("MT5_MOCK_APPROVAL_PROFILE", "preserve_10_ready")
    monkeypatch.setattr(MT5Connection, "_mock_runtime_state", lambda self: None)
    conn = MT5Connection(login=1, password="", server="mock")
    assert conn.connect(retry_count=1)
    return conn


def test_mt5_connection_snapshot_and_contract_are_usd(usc_mock):
    snapshot = usc_mock.get_account_snapshot()
    assert snapshot["balance"] == pytest.approx(10.0)
    assert snapshot["money_unit"] == "USD"
    assert snapshot["unit_scale"] == 0.01
    assert usc_mock.get_symbol_execution_contract("EURUSD").tick_value == pytest.approx(0.01)


def test_25_pip_1_5_percent_is_feasible_on_usc_mt5_connection(usc_mock):
    decision = usc_mock.evaluate_trade_feasibility("EURUSD", 0.015, 25.0)
    assert decision.approved, decision.details
    assert decision.estimated_lot == pytest.approx(0.06)


def test_mt5_connection_fixed_risk_eligibility_on_usc(usc_mock):
    decision = usc_mock.evaluate_fixed_risk_eligibility("EURUSD", 0.15, 25.0)
    assert decision.approved, decision.details
    assert decision.minimum_risk_usd == pytest.approx(0.025)


def test_default_mock_account_snapshot_has_no_unit_keys(monkeypatch):
    monkeypatch.delenv("MT5_MOCK_APPROVAL_PROFILE", raising=False)
    monkeypatch.setattr(MT5Connection, "_mock_runtime_state", lambda self: None)
    conn = MT5Connection(login=1, password="", server="mock")
    assert conn.connect(retry_count=1)
    snapshot = conn.get_account_snapshot()
    assert snapshot["balance"] == 10000.0
    assert "money_unit" not in snapshot and "unit_scale" not in snapshot


def test_deal_profit_cents_to_dollars_mt5_connection(usc_mock, monkeypatch):
    def deal(ticket, entry, deal_type, profit, comment):
        return SimpleNamespace(
            ticket=ticket, order=ticket, position_id=999, time_msc=1_793_000_000_000 + ticket * 1000,
            time=1_793_000_000 + ticket, type=deal_type, entry=entry, symbol="EURUSD", volume=10.0,
            price=1.1, profit=profit, commission=-70.0 if entry == 1 else 0.0, swap=0.0, fee=0.0,
            magic=1, comment=comment,
        )

    deals = [deal(1, 0, 0, 0.0, "AI_TEST_1"), deal(2, 1, 1, 5000.0, "")]
    monkeypatch.setattr(mt5_bridge.mt5, "history_deals_get", lambda start, end: deals, raising=False)
    assert usc_mock.get_position_history_summary(999)["profit_loss"] == pytest.approx(49.3)
    assert usc_mock.get_trade_history_summary("AI_TEST_1")["profit_loss"] == pytest.approx(49.3)


def test_preserve_10_facts_mt5_unchanged(usc_mock):
    facts = usc_mock.get_preserve_10_approval_facts()
    assert facts.can_assess, facts.details
    assert facts.account.normalized_balance_usd == 10.0
    assert facts.account.reported_balance == 1000.0
    assert facts.account.unit_scale == 0.01
    eurusd = facts.symbols["EURUSD"]
    assert eurusd.tick_value == 1.0  # raw, in account units
    assert eurusd.min_lot_margin == 25.0


# ----------------------------------------------------------------------
# ExecutionFeedbackReader
# ----------------------------------------------------------------------
def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _snapshot(currency: str | None) -> dict:
    payload = {
        "timestamp": _now().isoformat(), "balance": 1000.0, "equity": 1010.0, "margin_free": 990.0,
        "open_positions_count": 0, "floating_pnl": 10.0,
    }
    if currency is not None:
        payload["currency"] = currency
    return payload


def _execution(profit: float) -> dict:
    return {
        "trade_id": "AI_X", "ticket": 5, "status": "EXECUTED", "entry_price": 1.1, "lot_size": 0.01,
        "slippage": 0.0, "spread_at_entry": 1.0, "profit_loss": profit, "r_multiple": 0.0,
        "close_time": "2026-11-03T21:03:16Z",
    }


def _reader(tmp_path: Path) -> ExecutionFeedbackReader:
    return ExecutionFeedbackReader(tmp_path / "feedback", tmp_path / "exits")


def test_feedback_reader_converts_snapshot_and_exit_profit(tmp_path):
    reader = _reader(tmp_path)
    _write_json(tmp_path / "feedback" / "account_snapshot.json", _snapshot("USC"))
    exit_payload = json.loads(FIXTURE.read_text())
    exit_payload["profit_loss"] = 157651
    exit_path = tmp_path / "exits" / "exit_59159851261.json"
    _write_json(exit_path, exit_payload)
    _write_json(tmp_path / "feedback" / "execution_AI_X.json", _execution(4930.0))

    snapshot = reader.read_account_snapshot()
    assert snapshot["balance"] == pytest.approx(10.0)
    assert snapshot["equity"] == pytest.approx(10.1)
    assert snapshot["margin_free"] == pytest.approx(9.9)
    assert snapshot["floating_pnl"] == pytest.approx(0.1)
    assert snapshot["money_unit"] == "USD" and snapshot["unit_scale"] == 0.01

    [(path, converted)] = reader.collect_trade_exits()
    assert converted["profit_loss"] == pytest.approx(1576.51)
    [(_, execution)] = reader.collect_execution_feedback()
    assert execution["profit_loss"] == pytest.approx(49.3)
    # Nothing is written back: the files on disk still hold cents.
    assert json.loads(path.read_text())["profit_loss"] == 157651
    assert json.loads((tmp_path / "feedback" / "account_snapshot.json").read_text())["balance"] == 1000.0


def _exit_payload(profit: float = 157651) -> dict:
    payload = json.loads(FIXTURE.read_text())
    payload["profit_loss"] = profit
    return payload


def _unusable_snapshot(kind: str, tmp_path: Path, monkeypatch, reader: ExecutionFeedbackReader) -> None:
    snapshot_path = tmp_path / "feedback" / "account_snapshot.json"
    if kind == "missing":
        return
    if kind == "malformed":
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text("{not json", encoding="utf-8")
    elif kind == "currency_missing":
        _write_json(snapshot_path, _snapshot(None))
    elif kind == "currency_blank":
        _write_json(snapshot_path, _snapshot("  "))
    elif kind == "currency_not_text":
        _write_json(snapshot_path, _snapshot(None) | {"currency": 840})
    elif kind == "locked":
        _write_json(snapshot_path, _snapshot("USC"))
        reader._lock_retry_initial_delay_seconds = 0.0
        original = Path.read_text

        def locked(self, *args, **kwargs):
            if self.name == "account_snapshot.json":
                raise PermissionError("locked by the EA")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", locked)
    else:  # pragma: no cover
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["missing", "locked", "malformed", "currency_missing", "currency_blank",
                                  "currency_not_text"])
def test_feedback_reader_defers_everything_until_currency_is_known(tmp_path, monkeypatch, caplog, kind):
    """BR-001: with no usable currency, nothing is returned or deleted; the next poll can still process it."""
    reader = _reader(tmp_path)
    exit_path = tmp_path / "exits" / "exit_59159851261.json"
    execution_path = tmp_path / "feedback" / "execution_AI_X.json"
    _write_json(exit_path, _exit_payload())
    _write_json(execution_path, _execution(4930.0))
    _unusable_snapshot(kind, tmp_path, monkeypatch, reader)

    with caplog.at_level("WARNING", logger="fx_ai_engine.execution_feedback"):
        assert reader._unit_scale() is None
        assert reader.collect_trade_exits() == []
        assert reader.collect_execution_feedback() == []
        assert reader.consume_trade_exits() == []
        assert reader.consume_execution_feedback() == []
        assert reader.read_execution_feedback() == []
    assert "account currency unknown" in caplog.text
    assert exit_path.exists() and execution_path.exists()
    assert json.loads(exit_path.read_text())["profit_loss"] == 157651
    assert not (tmp_path / "exits" / "quarantine").exists()
    assert not (tmp_path / "feedback" / "quarantine").exists()

    monkeypatch.undo()
    _write_json(tmp_path / "feedback" / "account_snapshot.json", _snapshot("USC"))
    [(_, converted)] = reader.collect_trade_exits()
    assert converted["profit_loss"] == pytest.approx(1576.51)
    [(_, execution)] = reader.collect_execution_feedback()
    assert execution["profit_loss"] == pytest.approx(49.3)


def test_feedback_reader_usd_account_processes_feedback_on_the_first_poll(tmp_path):
    reader = _reader(tmp_path)
    assert reader._unit_scale() is None  # nothing read yet
    _write_json(tmp_path / "feedback" / "account_snapshot.json", _snapshot("USD"))
    _write_json(tmp_path / "exits" / "exit_59159851261.json", _exit_payload(12.5))
    _write_json(tmp_path / "feedback" / "execution_AI_X.json", _execution(7.25))
    [(_, exit_payload)] = reader.collect_trade_exits()
    [(_, execution)] = reader.collect_execution_feedback()
    assert exit_payload["profit_loss"] == 12.5
    assert execution["profit_loss"] == 7.25
    assert [p["profit_loss"] for p in reader.consume_execution_feedback()] == [7.25]
    assert not (tmp_path / "feedback" / "execution_AI_X.json").exists()


def test_feedback_reader_mock_simulator_artifacts_are_known_dollars(tmp_path):
    """The mock simulator writes dollars and no currency; it is accepted only when mock artifacts are allowed."""
    reader = _reader(tmp_path)
    mock_exit = _exit_payload(31.5) | {"feedback_source": "mock_feedback_simulator"}
    _write_json(tmp_path / "exits" / "exit_59159851261.json", mock_exit)
    _write_json(tmp_path / "feedback" / "execution_AI_X.json",
                _execution(0.0) | {"feedback_source": "mock_feedback_simulator"})
    _write_json(tmp_path / "feedback" / "account_snapshot.json",
                _snapshot(None) | {"snapshot_source": "mock_feedback_simulator"})
    [(_, payload)] = reader.collect_trade_exits()
    assert payload["profit_loss"] == 31.5
    assert len(reader.collect_execution_feedback()) == 1
    # A real EA artifact next to it still needs a currency.
    _write_json(tmp_path / "exits" / "exit_77.json", _exit_payload(5.0) | {"ticket": 77, "position_ticket": 77})
    assert [p["profit_loss"] for _, p in reader.collect_trade_exits()] == [31.5]
    # With mock artifacts disallowed the simulator payload is quarantined, never processed.
    strict = ExecutionFeedbackReader(tmp_path / "feedback", tmp_path / "exits", allow_mock_artifacts=False)
    assert strict.collect_trade_exits() == []
    assert (tmp_path / "exits" / "quarantine").exists()


def test_feedback_reader_usd_and_unknown_currency_are_identical_to_the_file(tmp_path):
    for currency in ("USD", "EUR"):
        reader = _reader(tmp_path / str(currency))
        payload = _snapshot(currency)
        _write_json(tmp_path / str(currency) / "feedback" / "account_snapshot.json", payload)
        assert reader.read_account_snapshot() == payload
        _write_json(tmp_path / str(currency) / "exits" / "exit_1.json",
                    {"ticket": 1, "profit_loss": 12.5, "status": "CLOSED_WIN", "close_time": "x"})
        [(_, exit_payload)] = reader.collect_trade_exits()
        assert exit_payload["profit_loss"] == 12.5


def test_read_execution_feedback_returns_dollars_and_keeps_the_file(tmp_path):
    """BR-003: the public non-deleting reader goes through the same normalising path."""
    reader = _reader(tmp_path)
    path = tmp_path / "feedback" / "execution_AI_X.json"
    _write_json(path, _execution(4930.0))
    assert reader.read_execution_feedback() == []  # currency unknown: deferred
    _write_json(tmp_path / "feedback" / "account_snapshot.json", _snapshot("USC"))
    [execution] = reader.read_execution_feedback()
    assert execution["profit_loss"] == pytest.approx(49.3)
    assert path.exists() and json.loads(path.read_text())["profit_loss"] == 4930.0


def test_feedback_reader_keeps_last_good_scale_when_snapshot_disappears(tmp_path):
    reader = _reader(tmp_path)
    snapshot_path = tmp_path / "feedback" / "account_snapshot.json"
    _write_json(snapshot_path, _snapshot("USC"))
    assert reader._unit_scale() == 0.01
    snapshot_path.unlink()
    assert reader._unit_scale() == 0.01


# ----------------------------------------------------------------------
# Engine end to end on a USC bridge
# ----------------------------------------------------------------------
def test_usc_engine_end_to_end(tmp_path, monkeypatch, isolated_db):
    usd_dir = build_bridge(tmp_path / "usd", _now(), open_tickets=())
    deals_path = usd_dir / "market" / "deals.json"
    deals = json.loads(deals_path.read_text())
    deals["deals"].extend(LIVE_DEALS)
    deals_path.write_text(json.dumps(deals))
    shutil.copyfile(FIXTURE, usd_dir / "exits" / FIXTURE.name)
    usc_dir = convert_bridge_to_usc(usd_dir, tmp_path / "usc", balance_usd=100000.0)

    raw_exit = json.loads((usc_dir / "exits" / FIXTURE.name).read_text())
    assert raw_exit["profit_loss"] == 157651
    assert json.loads((usc_dir / "feedback" / "account_snapshot.json").read_text())["balance"] == 10_000_000.0

    engine = make_file_bridge_engine(usc_dir, monkeypatch)
    _seed_trade(isolated_db, engine.evidence_context)
    engine._consume_feedback()
    trade = _trade(isolated_db)
    assert trade["status"] == "CLOSED_WIN"
    assert trade["profit_loss"] == pytest.approx(1576.51)
    assert trade["trade_id"] == TRADE_ID

    engine._update_account_state()
    assert engine.account_status.balance == pytest.approx(100000.0)
    assert engine.account_status.state_reconciled, engine.account_status.state_reconciliation_reason


# ----------------------------------------------------------------------
# BR-002: unknown currency is not scale 1.0 (history and contract readers)
# ----------------------------------------------------------------------
CURRENCY_UNKNOWN = "FILE_BRIDGE_ACCOUNT_CURRENCY_UNKNOWN"


def _assert_history_and_contract_unavailable(conn: FileBridgeConnection, *, contract_code: str | None = CURRENCY_UNKNOWN) -> None:
    assert conn.get_position_history_summary(999) is None
    assert conn.last_error.code == CURRENCY_UNKNOWN
    conn.last_error = None
    assert conn.get_trade_history_summary("AI_TEST_1") is None
    assert conn.last_error.code == CURRENCY_UNKNOWN
    conn.last_error = None
    assert conn.get_symbol_execution_contract("EURUSD") is None
    if contract_code is not None:
        assert conn.last_error.code == contract_code


def test_file_bridge_history_without_currency_evidence_returns_nothing_then_recovers(tmp_path):
    bridge = _usc_bridge(tmp_path)
    snapshot_path = bridge / "feedback" / "account_snapshot.json"
    saved = snapshot_path.read_text()
    snapshot_path.unlink()
    conn = FileBridgeConnection(bridge)  # fresh: nothing cached, status.json carries no account_currency
    _assert_history_and_contract_unavailable(conn)
    snapshot_path.write_text(saved)  # evidence returns: the next poll converts
    assert conn.get_position_history_summary(999)["profit_loss"] == pytest.approx(49.3)
    assert conn.get_trade_history_summary("AI_TEST_1")["profit_loss"] == pytest.approx(49.3)
    assert conn.get_symbol_execution_contract("EURUSD").tick_value == pytest.approx(0.01)


def test_file_bridge_history_with_stale_currency_evidence_returns_nothing(tmp_path):
    from datetime import timedelta

    bridge = _usc_bridge(tmp_path)
    # Deals stay fresh (120 s) while status.json and the snapshot are stale (30 s).
    conn = FileBridgeConnection(bridge, clock=lambda: _now() + timedelta(seconds=60))
    # The contract reader also needs a fresh status.json, which fails first.
    _assert_history_and_contract_unavailable(conn, contract_code="FILE_BRIDGE_SYMBOL_SPEC_UNAVAILABLE")


def test_file_bridge_history_uses_only_a_validated_cached_currency(tmp_path):
    from datetime import timedelta

    bridge = _usc_bridge(tmp_path)
    offset = [0]
    conn = FileBridgeConnection(bridge, clock=lambda: _now() + timedelta(seconds=offset[0]))
    assert conn.connect(retry_count=1), conn.last_error
    assert conn.get_account_snapshot()["balance"] == pytest.approx(10.0)  # validated snapshot caches USC
    offset[0] = 60
    assert conn.get_position_history_summary(999)["profit_loss"] == pytest.approx(49.3)


def test_file_bridge_history_with_conflicting_currencies_returns_nothing(tmp_path):
    bridge = _usc_bridge(tmp_path)
    status_path = bridge / "market" / "status.json"
    status = json.loads(status_path.read_text())
    status["account_currency"] = "USD"  # snapshot says USC
    status_path.write_text(json.dumps(status))
    _assert_history_and_contract_unavailable(FileBridgeConnection(bridge))


def _edit_json(path: Path, **changes) -> None:
    payload = json.loads(path.read_text())
    payload.update(changes)
    path.write_text(json.dumps(payload))
REJECTED_SNAPSHOTS = {
    "wrong_login": ("feedback/account_snapshot.json", {"login": 1}),
    "wrong_server": ("feedback/account_snapshot.json", {"server": "Other-Server"}),
    "not_authenticated": ("feedback/account_snapshot.json", {"account_authenticated": False}),
    "terminal_disconnected": ("feedback/account_snapshot.json", {"terminal_connected": False}),
    "account_type_differs": ("feedback/account_snapshot.json", {"account_is_demo": True, "_status_demo": False}),
    "balance_not_finite": ("feedback/account_snapshot.json", {"balance": "NaN"}),
    "equity_missing": ("feedback/account_snapshot.json", {"equity": None}),
    "status_server_offset_invalid": ("market/status.json", {"server_offset_valid": False}),
    "status_terminal_disconnected": ("market/status.json", {"terminal_connected": False}),
}
@pytest.mark.parametrize("case", sorted(REJECTED_SNAPSHOTS))
def test_file_bridge_history_rejects_currency_from_an_invalid_fresh_snapshot_then_recovers(tmp_path, case):
    bridge = _usc_bridge(tmp_path)
    snapshot_path = bridge / "feedback" / "account_snapshot.json"
    status_path = bridge / "market" / "status.json"
    good_snapshot, good_status = snapshot_path.read_text(), status_path.read_text()
    relative, changes = REJECTED_SNAPSHOTS[case]
    changes = dict(changes)
    if "_status_demo" in changes:  # snapshot and status disagree on the account type
        _edit_json(status_path, account_is_demo=changes.pop("_status_demo"))
        changes["account_is_demo"] = not json.loads(status_path.read_text())["account_is_demo"]
    _edit_json(bridge / relative, **changes)
    conn = FileBridgeConnection(bridge)
    assert conn._account_currency() is None
    _assert_history_and_contract_unavailable(conn, contract_code=None)
    snapshot_path.write_text(good_snapshot)
    status_path.write_text(good_status)
    assert conn.get_position_history_summary(999)["profit_loss"] == pytest.approx(49.3)
    assert conn.get_trade_history_summary("AI_TEST_1")["profit_loss"] == pytest.approx(49.3)
def test_file_bridge_invalid_fresh_snapshot_ignores_a_previously_cached_currency(tmp_path):
    bridge = _usc_bridge(tmp_path)
    conn = _connect(bridge)
    assert conn.get_account_snapshot()["balance"] == pytest.approx(10.0)  # validated snapshot caches USC
    _edit_json(bridge / "feedback" / "account_snapshot.json", login=1)  # a different account answers
    assert conn.get_position_history_summary(999) is None
    assert conn.last_error.code == CURRENCY_UNKNOWN
    assert conn.get_trade_history_summary("AI_TEST_1") is None
def test_file_bridge_status_currency_alone_is_not_evidence_and_is_not_cached(tmp_path):
    bridge = _usc_bridge(tmp_path)
    snapshot_path = bridge / "feedback" / "account_snapshot.json"
    saved = snapshot_path.read_text()
    snapshot_path.unlink()
    _edit_json(bridge / "market" / "status.json", account_currency="USC")
    conn = _connect(bridge)
    assert conn._account_currency() is None
    _assert_history_and_contract_unavailable(conn)
    snapshot_path.write_text(saved)
    assert conn.get_position_history_summary(999)["profit_loss"] == pytest.approx(49.3)
def test_file_bridge_history_still_works_when_only_the_symbols_gate_fails(tmp_path):
    bridge = _usc_bridge(tmp_path)
    _edit_json(bridge / "market" / "status.json", symbols_ok=False, symbol_errors=["EURUSD"])
    conn = FileBridgeConnection(bridge)
    assert conn.get_position_history_summary(999)["profit_loss"] == pytest.approx(49.3)
    assert conn.get_trade_history_summary("AI_TEST_1")["profit_loss"] == pytest.approx(49.3)
    assert conn.get_symbol_execution_contract("EURUSD") is None  # the contract reader stays gated
    assert conn.last_error.code == "FILE_BRIDGE_SYMBOLS_NOT_OK"
def _usc_deals():
    def deal(ticket, entry, deal_type, profit, comment):
        return SimpleNamespace(
            ticket=ticket, order=ticket, position_id=999, time_msc=1_793_000_000_000 + ticket * 1000,
            time=1_793_000_000 + ticket, type=deal_type, entry=entry, symbol="EURUSD", volume=10.0,
            price=1.1, profit=profit, commission=-70.0 if entry == 1 else 0.0, swap=0.0, fee=0.0,
            magic=1, comment=comment,
        )

    return [deal(1, 0, 0, 0.0, "AI_TEST_1"), deal(2, 1, 1, 5000.0, "")]


@pytest.mark.parametrize("failure", ["none", "raises", "no_currency", "blank_currency"])
def test_mt5_connection_history_without_currency_returns_nothing_then_recovers(usc_mock, monkeypatch, failure):
    monkeypatch.setattr(mt5_bridge.mt5, "history_deals_get", lambda start, end: _usc_deals(), raising=False)
    healthy = usc_mock._account_info

    def broken():
        if failure == "none":
            return None
        if failure == "raises":
            raise RuntimeError("account_info unavailable")
        return SimpleNamespace(balance=1000.0, equity=1000.0, margin_free=1000.0,
                               currency=None if failure == "no_currency" else "  ")

    monkeypatch.setattr(usc_mock, "_account_info", broken)
    assert usc_mock._unit_scale() is None
    assert usc_mock.get_position_history_summary(999) is None
    assert usc_mock.last_error.code == "MT5_ACCOUNT_CURRENCY_UNAVAILABLE"
    usc_mock.last_error = None
    assert usc_mock.get_trade_history_summary("AI_TEST_1") is None
    assert usc_mock.last_error.code == "MT5_ACCOUNT_CURRENCY_UNAVAILABLE"
    usc_mock.last_error = None
    assert usc_mock.get_symbol_execution_contract("EURUSD") is None
    assert usc_mock.last_error.code == "MT5_ACCOUNT_CURRENCY_UNAVAILABLE"

    monkeypatch.setattr(usc_mock, "_account_info", healthy)  # next poll
    assert usc_mock.get_position_history_summary(999)["profit_loss"] == pytest.approx(49.3)
    assert usc_mock.get_trade_history_summary("AI_TEST_1")["profit_loss"] == pytest.approx(49.3)


# ----------------------------------------------------------------------
# BR-006: bridge health prints dollars
# ----------------------------------------------------------------------
def _health_bridge(tmp_path: Path, payload: dict) -> Path:
    _write_json(tmp_path / "feedback" / "account_snapshot.json", payload)
    return tmp_path


def test_bridge_health_balance_is_normalised_to_dollars(tmp_path):
    from check_bridge_health import check_snapshot_health

    assert check_snapshot_health(_health_bridge(tmp_path / "usc", _snapshot("USC") | {"balance": 1000.0}))["balance"] == pytest.approx(10.0)
    assert check_snapshot_health(_health_bridge(tmp_path / "usd", _snapshot("USD") | {"balance": 1000.0}))["balance"] == 1000.0
    assert check_snapshot_health(_health_bridge(tmp_path / "eur", _snapshot("EUR") | {"balance": 1000.0}))["balance"] == 1000.0
    unknown = check_snapshot_health(_health_bridge(tmp_path / "none", _snapshot(None)))
    assert unknown["status"] == "FRESH" and unknown["balance"] is None  # currency unknown: no figure
    mock = _snapshot(None) | {"snapshot_source": "mock_feedback_simulator", "balance": 950.0}
    assert check_snapshot_health(_health_bridge(tmp_path / "mock", mock))["balance"] == 950.0


def test_bridge_health_cli_prints_ten_dollars_for_a_thousand_cents(tmp_path, monkeypatch, capsys):
    import check_bridge_health
    import core.bridge_utils as bridge_utils

    bridge = _health_bridge(tmp_path, _snapshot("USC") | {"balance": 1000.0})
    monkeypatch.setenv("USE_MT5_MOCK", "1")
    monkeypatch.setattr(bridge_utils, "get_mt5_bridge_path", lambda: bridge)
    check_bridge_health.main()
    out = capsys.readouterr().out
    assert "Balance:     $10.00" in out
    assert "$1000.00" not in out
