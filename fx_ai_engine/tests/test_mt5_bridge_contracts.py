from __future__ import annotations

import types
from datetime import datetime, timezone

from core.mt5_bridge import MT5Connection


SRS_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF")
NOW_UTC = datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)


class _FailingInitMT5:
    def initialize(self, login: int, server: str, password: str) -> bool:
        return False

    def last_error(self):
        return (10001, "init failed")


class _SymbolUnavailableMT5:
    TIMEFRAME_M15 = 15

    def initialize(self, login: int, server: str, password: str) -> bool:
        return True

    def last_error(self):
        return (5001, "symbol unavailable")

    def account_info(self):
        return types.SimpleNamespace(balance=1000.0, equity=1000.0, margin_free=900.0)

    def positions_get(self):
        return []

    def symbol_select(self, symbol: str, select: bool) -> bool:
        return False

    def shutdown(self) -> None:
        return None


class _FeasibleLotMT5(_SymbolUnavailableMT5):
    def symbol_select(self, symbol: str, select: bool) -> bool:
        return True

    def symbol_info(self, symbol: str):
        return types.SimpleNamespace(
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            trade_tick_value=0.1,
            trade_tick_size=0.00001,
            point=0.00001,
        )


class _InfeasibleLotMT5(_FeasibleLotMT5):
    def symbol_info(self, symbol: str):
        return types.SimpleNamespace(
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            trade_tick_value=1.0,
            trade_tick_size=0.00001,
            point=0.00001,
        )


class _ApprovalFactsMT5(_FeasibleLotMT5):
    ORDER_TYPE_BUY = 0
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_TRADE_MODE_FULL = 4

    def __init__(self):
        quote_time = int(NOW_UTC.timestamp()) - 30
        self.account = types.SimpleNamespace(
            balance=1000.0,
            equity=1005.0,
            margin_free=900.0,
            currency="USC",
            leverage=500,
            trade_allowed=True,
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
        )
        self.symbols = {
            symbol: types.SimpleNamespace(
                trade_mode=self.SYMBOL_TRADE_MODE_FULL,
                volume_min=0.01,
                volume_step=0.01,
                volume_max=100.0,
                trade_contract_size=100000.0,
                trade_tick_value=0.1,
                trade_tick_size=0.00001,
                point=0.00001 if not symbol.endswith("JPY") else 0.001,
                digits=5 if not symbol.endswith("JPY") else 3,
                trade_stops_level=5,
                trade_freeze_level=0,
                time=quote_time,
            )
            for symbol in SRS_SYMBOLS
        }
        self.ticks = {
            symbol: types.SimpleNamespace(
                bid=1.1000 if not symbol.endswith("JPY") else 150.000,
                ask=1.1002 if not symbol.endswith("JPY") else 150.020,
                time=quote_time,
            )
            for symbol in SRS_SYMBOLS
        }
        self.margin_by_symbol = {symbol: 0.25 for symbol in SRS_SYMBOLS}

    def account_info(self):
        return self.account

    def symbol_info(self, symbol: str):
        return self.symbols.get(symbol)

    def symbol_info_tick(self, symbol: str):
        return self.ticks.get(symbol)

    def order_calc_margin(self, order_type: int, symbol: str, volume: float, price: float) -> float:
        assert order_type == self.ORDER_TYPE_BUY
        assert volume > 0
        assert price > 0
        return self.margin_by_symbol.get(symbol, 0.0)


class _CentSnapshotMT5(_ApprovalFactsMT5):
    def __init__(self):
        super().__init__()
        self.positions = [types.SimpleNamespace(symbol="EURUSD", profit=250.0)]

    def positions_get(self):
        return self.positions


class _CentInfeasibleMT5(_ApprovalFactsMT5):
    def symbol_info(self, symbol: str):
        return types.SimpleNamespace(
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            trade_tick_value=10.0,
            trade_tick_size=0.00001,
            point=0.00001,
        )


class _RealAccountMT5(_ApprovalFactsMT5):
    def __init__(self):
        super().__init__()
        self.account.trade_mode = self.ACCOUNT_TRADE_MODE_REAL


class _HistorySummaryMT5(_FeasibleLotMT5):
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3

    def __init__(self):
        self.positions = [types.SimpleNamespace(ticket=880002, symbol="GBPUSD", profit=0.0)]
        self.deals = [
            types.SimpleNamespace(
                ticket=700001,
                order=900001,
                time=int(NOW_UTC.timestamp()),
                time_msc=int(NOW_UTC.timestamp() * 1000),
                type=self.ORDER_TYPE_SELL,
                entry=self.DEAL_ENTRY_IN,
                volume=0.34,
                price=1.35789,
                position_id=880001,
                profit=0.0,
                comment="AI_hist_001",
                symbol="USDCAD",
            ),
            types.SimpleNamespace(
                ticket=700002,
                order=900002,
                time=int(NOW_UTC.timestamp()) + 600,
                time_msc=int((NOW_UTC.timestamp() + 600) * 1000),
                type=self.ORDER_TYPE_BUY,
                entry=self.DEAL_ENTRY_OUT,
                volume=0.34,
                price=1.35900,
                position_id=880001,
                profit=-25.5,
                comment="",
                symbol="USDCAD",
            ),
        ]

    def positions_get(self):
        return self.positions

    def history_deals_get(self, _start, _end):
        return self.deals


class _PartialHistorySummaryMT5(_FeasibleLotMT5):
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3

    def __init__(self):
        self.positions = []
        now = int(NOW_UTC.timestamp())
        self.deals = [
            types.SimpleNamespace(
                ticket=710001,
                order=910001,
                time=now,
                time_msc=now * 1000,
                type=self.ORDER_TYPE_BUY,
                entry=self.DEAL_ENTRY_IN,
                volume=1.0,
                price=1.1000,
                position_id=880003,
                profit=0.0,
                comment="AI_hist_partial_001",
                symbol="EURUSD",
            ),
            types.SimpleNamespace(
                ticket=710002,
                order=910002,
                time=now + 300,
                time_msc=(now + 300) * 1000,
                type=self.ORDER_TYPE_SELL,
                entry=self.DEAL_ENTRY_OUT,
                volume=0.5,
                price=1.1020,
                position_id=880003,
                profit=20.0,
                comment="",
                symbol="EURUSD",
            ),
            types.SimpleNamespace(
                ticket=710003,
                order=910003,
                time=now + 600,
                time_msc=(now + 600) * 1000,
                type=self.ORDER_TYPE_SELL,
                entry=self.DEAL_ENTRY_OUT,
                volume=0.5,
                price=1.1010,
                position_id=880003,
                profit=10.0,
                comment="",
                symbol="EURUSD",
            ),
        ]

    def positions_get(self):
        return self.positions

    def history_deals_get(self, _start, _end):
        return self.deals


class _ChargedPartialHistorySummaryMT5(_FeasibleLotMT5):
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3

    def __init__(self):
        self.positions = []
        now = int(NOW_UTC.timestamp())
        self.deals = [
            types.SimpleNamespace(
                ticket=720001,
                order=920001,
                time=now,
                time_msc=now * 1000,
                type=self.ORDER_TYPE_BUY,
                entry=self.DEAL_ENTRY_IN,
                volume=1.0,
                price=1.1000,
                position_id=880004,
                profit=0.0,
                fee=0.0,
                swap=0.0,
                commission=0.0,
                comment="AI_hist_charged_001",
                symbol="EURUSD",
            ),
            types.SimpleNamespace(
                ticket=720002,
                order=920002,
                time=now + 300,
                time_msc=(now + 300) * 1000,
                type=self.ORDER_TYPE_SELL,
                entry=self.DEAL_ENTRY_OUT,
                volume=0.4,
                price=1.1015,
                position_id=880004,
                profit=12.0,
                fee=-0.5,
                swap=0.0,
                commission=-1.0,
                comment="",
                symbol="EURUSD",
            ),
            types.SimpleNamespace(
                ticket=720003,
                order=920003,
                time=now + 600,
                time_msc=(now + 600) * 1000,
                type=self.ORDER_TYPE_SELL,
                entry=self.DEAL_ENTRY_OUT,
                volume=0.6,
                price=1.0990,
                position_id=880004,
                profit=5.0,
                fee=-0.5,
                swap=-20.0,
                commission=-2.0,
                comment="",
                symbol="EURUSD",
            ),
        ]

    def positions_get(self):
        return self.positions

    def history_deals_get(self, _start, _end):
        return self.deals


def test_connect_failure_sets_explicit_error(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _FailingInitMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    ok = bridge.connect()

    assert ok is False
    assert bridge.last_error is not None
    assert bridge.last_error.code == "MT5_INIT_FAILED"


def test_ohlc_failure_sets_error_attr(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _SymbolUnavailableMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    df = bridge.fetch_ohlc_data("EURUSD", timeframe=15, num_candles=10)

    assert df.empty
    assert "error" in df.attrs
    assert df.attrs["error"]["code"] == "SYMBOL_SELECT_FAILED"


def test_trade_feasibility_rejects_when_raw_lot_is_below_broker_minimum(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _InfeasibleLotMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    decision = bridge.evaluate_trade_feasibility(
        "EURUSD",
        risk_percent=0.05,
        stop_pips=20.0,
        account_balance=10.0,
    )

    assert decision.can_assess is True
    assert decision.approved is False
    assert decision.reason_code == "REJECTED_LOT_PREROUTE"
    assert "min_lot=0.0100" in decision.details


def test_fixed_risk_eligibility_rejects_when_configured_risk_cannot_fund_minimum_lot(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _InfeasibleLotMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    decision = bridge.evaluate_fixed_risk_eligibility(
        "EURUSD",
        fixed_risk_usd=0.50,
        stop_pips=20.0,
        account_balance=10.0,
    )

    assert decision.can_assess is True
    assert decision.approved is False
    assert decision.reason_code == "STRATEGIC_RISK_INELIGIBLE"
    assert decision.minimum_risk_usd > 0.5
    assert "minimum_risk_usd=" in decision.details


def test_fixed_risk_eligibility_approves_when_configured_risk_can_fund_minimum_lot(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _FeasibleLotMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    decision = bridge.evaluate_fixed_risk_eligibility(
        "EURUSD",
        fixed_risk_usd=2.50,
        stop_pips=20.0,
        account_balance=10.0,
    )

    assert decision.can_assess is True
    assert decision.approved is True
    assert decision.reason_code == "STRATEGIC_RISK_ELIGIBLE"
    assert decision.minimum_risk_usd > 0


def test_trade_feasibility_marks_unavailable_contract_as_unassessable(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _SymbolUnavailableMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    decision = bridge.evaluate_trade_feasibility(
        "EURUSD",
        risk_percent=0.05,
        stop_pips=20.0,
        account_balance=10.0,
    )

    assert decision.can_assess is False
    assert decision.approved is True
    assert decision.reason_code == "BROKER_CONTRACT_UNAVAILABLE"


def test_trade_feasibility_approves_when_estimated_lot_meets_minimum(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _FeasibleLotMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    decision = bridge.evaluate_trade_feasibility(
        "EURUSD",
        risk_percent=0.05,
        stop_pips=20.0,
        account_balance=10.0,
    )

    assert decision.can_assess is True
    assert decision.approved is True
    assert decision.reason_code == "TRADE_FEASIBLE"
    assert decision.estimated_lot >= 0.01


def test_preserve_10_approval_facts_normalize_cent_account_and_all_srs_symbols(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _ApprovalFactsMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    facts = bridge.get_preserve_10_approval_facts(now=NOW_UTC)

    assert facts.can_assess is True
    assert facts.reason_code == "APPROVAL_FACTS_READY"
    assert facts.account is not None
    assert facts.account.currency == "USC"
    assert facts.account.denomination == "usd_cent"
    assert facts.account.normalized_balance_usd == 10.0
    assert set(facts.symbols) == set(SRS_SYMBOLS)
    assert facts.symbols["EURUSD"].quote_age_seconds == 30
    assert facts.symbols["USDJPY"].spread_pips == 2.0


def test_is_demo_account_detects_demo_and_real_trade_modes(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _ApprovalFactsMT5())
    demo_bridge = MT5Connection(login=123, password="x", server="demo")
    assert demo_bridge.connect() is True
    assert demo_bridge.is_demo_account() is True

    monkeypatch.setattr(bridge_mod, "mt5", _RealAccountMT5())
    real_bridge = MT5Connection(login=123, password="x", server="real")
    assert real_bridge.connect() is True
    assert real_bridge.is_demo_account() is False


def test_preserve_10_approval_facts_fail_closed_when_symbol_info_missing(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    mt5 = _ApprovalFactsMT5()
    mt5.symbols["USDCHF"] = None
    monkeypatch.setattr(bridge_mod, "mt5", mt5)

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    facts = bridge.get_preserve_10_approval_facts(now=NOW_UTC)

    assert facts.can_assess is False
    assert facts.reason_code == "APPROVAL_SYMBOL_INFO_MISSING"
    assert "USDCHF" in facts.details
    assert facts.account is not None
    assert facts.symbols


def test_preserve_10_approval_facts_fail_closed_when_quote_is_stale(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    mt5 = _ApprovalFactsMT5()
    mt5.ticks["GBPUSD"] = types.SimpleNamespace(bid=1.2000, ask=1.2002, time=int(NOW_UTC.timestamp()) - 600)
    monkeypatch.setattr(bridge_mod, "mt5", mt5)

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    facts = bridge.get_preserve_10_approval_facts(now=NOW_UTC, max_quote_age_seconds=120)

    assert facts.can_assess is False
    assert facts.reason_code == "APPROVAL_SYMBOL_TICK_STALE"
    assert "GBPUSD" in facts.details


def test_preserve_10_approval_facts_fail_closed_when_symbol_facts_are_inconsistent(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    mt5 = _ApprovalFactsMT5()
    mt5.ticks["AUDUSD"] = types.SimpleNamespace(bid=0.7003, ask=0.7001, time=int(NOW_UTC.timestamp()) - 15)
    monkeypatch.setattr(bridge_mod, "mt5", mt5)

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    facts = bridge.get_preserve_10_approval_facts(now=NOW_UTC)

    assert facts.can_assess is False
    assert facts.reason_code == "APPROVAL_SYMBOL_FACTS_INCONSISTENT"
    assert "AUDUSD" in facts.details


def test_get_account_snapshot_normalizes_cent_balances_and_floating_pnl(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _CentSnapshotMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    snapshot = bridge.get_account_snapshot()

    assert snapshot["balance"] == 10.0
    assert snapshot["equity"] == 10.05
    assert snapshot["margin_free"] == 9.0
    assert snapshot["floating_pnl"] == 2.5


def test_trade_feasibility_normalizes_cent_balance_when_omitted(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _CentInfeasibleMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    decision = bridge.evaluate_trade_feasibility("EURUSD", risk_percent=0.05, stop_pips=20.0)

    assert decision.can_assess is True
    assert decision.approved is False
    assert decision.reason_code == "REJECTED_LOT_PREROUTE"


def test_position_history_summary_and_open_position_tickets(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _HistorySummaryMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    assert bridge.get_open_position_tickets() == [880002]
    summary = bridge.get_position_history_summary(880001)

    assert summary is not None
    assert summary["trade_id"] == "AI_hist_001"
    assert summary["trade_ticket"] == 900001
    assert summary["position_ticket"] == 880001
    assert summary["direction"] == "SELL"
    assert summary["entry_price"] == 1.35789
    assert summary["close_price"] == 1.35900
    assert summary["lot_size"] == 0.34
    assert summary["profit_loss"] == -25.5
    assert summary["status"] == "CLOSED_LOSS"
    assert summary["close_deals_count"] == 1
    assert summary["close_volume"] == 0.34
    assert summary["close_legs"][0]["profit_loss"] == -25.5


def test_trade_history_summary_by_trade_id(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _HistorySummaryMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    summary = bridge.get_trade_history_summary("AI_hist_001")

    assert summary is not None
    assert summary["trade_id"] == "AI_hist_001"
    assert summary["trade_ticket"] == 900001
    assert summary["position_ticket"] == 880001
    assert summary["direction"] == "SELL"
    assert summary["entry_price"] == 1.35789
    assert summary["close_price"] == 1.35900
    assert summary["lot_size"] == 0.34
    assert summary["profit_loss"] == -25.5
    assert summary["status"] == "CLOSED_LOSS"
    assert summary["close_deals_count"] == 1


def test_position_history_summary_accumulates_partial_close_deals(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _PartialHistorySummaryMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    summary = bridge.get_position_history_summary(880003)

    assert summary is not None
    assert summary["trade_id"] == "AI_hist_partial_001"
    assert summary["position_ticket"] == 880003
    assert summary["entry_price"] == 1.1000
    assert summary["close_price"] == 1.1010
    assert summary["lot_size"] == 1.0
    assert summary["profit_loss"] == 30.0
    assert summary["status"] == "CLOSED_WIN"
    assert summary["close_deals_count"] == 2
    assert summary["close_volume"] == 1.0
    assert [leg["profit_loss"] for leg in summary["close_legs"]] == [20.0, 10.0]


def test_position_history_summary_aggregates_exit_charges_across_partial_closes(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _ChargedPartialHistorySummaryMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    summary = bridge.get_position_history_summary(880004)

    assert summary is not None
    assert summary["trade_id"] == "AI_hist_charged_001"
    assert summary["position_ticket"] == 880004
    assert summary["close_price"] == 1.0990
    assert summary["profit_loss"] == -7.0
    assert summary["status"] == "CLOSED_LOSS"
    assert summary["close_deals_count"] == 2
    assert [leg["profit_loss"] for leg in summary["close_legs"]] == [10.5, -17.5]


def test_trade_history_summary_aggregates_exit_charges_across_partial_closes(monkeypatch) -> None:
    import core.mt5_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "mt5", _ChargedPartialHistorySummaryMT5())

    bridge = MT5Connection(login=123, password="x", server="demo")
    assert bridge.connect() is True

    summary = bridge.get_trade_history_summary("AI_hist_charged_001")

    assert summary is not None
    assert summary["trade_id"] == "AI_hist_charged_001"
    assert summary["trade_ticket"] == 920001
    assert summary["position_ticket"] == 880004
    assert summary["profit_loss"] == -7.0
    assert summary["status"] == "CLOSED_LOSS"
    assert summary["close_deals_count"] == 2
    assert [leg["profit_loss"] for leg in summary["close_legs"]] == [10.5, -17.5]
