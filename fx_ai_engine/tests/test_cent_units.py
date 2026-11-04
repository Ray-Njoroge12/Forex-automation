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


def test_feedback_reader_without_snapshot_leaves_profit_unchanged(tmp_path):
    reader = _reader(tmp_path)
    assert reader._unit_scale() == 1.0
    exit_payload = json.loads(FIXTURE.read_text())
    exit_payload["profit_loss"] = 157651
    _write_json(tmp_path / "exits" / "exit_59159851261.json", exit_payload)
    [(_, unchanged)] = reader.collect_trade_exits()
    assert unchanged["profit_loss"] == 157651


def test_feedback_reader_usd_and_unknown_currency_are_identical_to_the_file(tmp_path):
    for currency in ("USD", "EUR", None):
        reader = _reader(tmp_path / str(currency))
        payload = _snapshot(currency)
        _write_json(tmp_path / str(currency) / "feedback" / "account_snapshot.json", payload)
        assert reader.read_account_snapshot() == payload
        _write_json(tmp_path / str(currency) / "exits" / "exit_1.json",
                    {"ticket": 1, "profit_loss": 12.5, "status": "CLOSED_WIN", "close_time": "x"})
        [(_, exit_payload)] = reader.collect_trade_exits()
        assert exit_payload["profit_loss"] == 12.5


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
