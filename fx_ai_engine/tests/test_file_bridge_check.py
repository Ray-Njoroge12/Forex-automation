"""validation.file_bridge_check rows for cent units and the EA symbol/profile fields (C1)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.bridge_fixtures import (
    SUFFIXED_PROFILE,
    build_full_usd_bridge,
    convert_bridge_to_suffixed,
    convert_bridge_to_usc,
)
from validation.file_bridge_check import normalise_margin_mode, run_checks

PROFILE_ROWS = {"symbols_ok", "symbol_suffix", "account_margin_mode", "ea_profile"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _usd(tmp_path: Path) -> Path:
    return build_full_usd_bridge(tmp_path / "usd", _now())


def _rows(bridge: Path) -> dict:
    return {check.check_id: check for check in run_checks(bridge)}


def _suffixed(tmp_path: Path, **kwargs) -> Path:
    profile = {**SUFFIXED_PROFILE, **kwargs.pop("profile", {})}
    return convert_bridge_to_suffixed(_usd(tmp_path), tmp_path / "sfx", profile=profile, **kwargs)


def test_old_ea_usd_bridge_has_no_extra_rows(tmp_path):
    rows = _rows(_usd(tmp_path))
    assert all(check.approved for check in rows.values()), [c for c in rows.values() if not c.approved]
    assert not PROFILE_ROWS & set(rows)
    assert "unit_scale" not in rows["account_snapshot"].details
    assert "currency=" not in rows["account_snapshot"].details
    assert rows["symbol_spec_EURUSD"].details == "min_lot=0.01 step=0.01 tick_value=1.0"


def test_usc_bridge_shows_unit_scale_and_raw_tick_value(tmp_path):
    usc = convert_bridge_to_usc(_usd(tmp_path), tmp_path / "usc", balance_usd=10.0)
    rows = _rows(usc)
    assert all(check.approved for check in rows.values()), [c for c in rows.values() if not c.approved]
    assert rows["account_snapshot"].details.endswith(" currency=USC unit_scale=0.01 (values in USD)")
    assert "balance=10.0" in rows["account_snapshot"].details
    assert "tick_value_usd=0.01 tick_value_raw=1.0" in rows["symbol_spec_EURUSD"].details
    assert not PROFILE_ROWS & set(rows)


def test_suffixed_bridge_passes_and_prints_profile(tmp_path):
    rows = _rows(_suffixed(tmp_path))
    assert all(check.approved for check in rows.values()), [c for c in rows.values() if not c.approved]
    assert rows["symbols_ok"].approved
    assert "EURUSD->EURUSDc" in rows["symbol_suffix"].details and "suffix='c'" in rows["symbol_suffix"].details
    assert rows["account_margin_mode"].details == "account_margin_mode=HEDGING"
    assert rows["ea_profile"].details == "account_currency=USD max_spread_pips_input=2.0 live_guard={'enabled': False}"


def test_symbols_not_ok_fails_with_code_and_symbols(tmp_path):
    rows = _rows(_suffixed(tmp_path, symbols_ok=False, symbol_errors=("EURUSD", "GBPUSD")))
    row = rows["symbols_ok"]
    assert not row.approved
    assert "FILE_BRIDGE_SYMBOLS_NOT_OK" in row.details and "EURUSD, GBPUSD" in row.details
    assert not rows["ea_status"].approved
    assert "account_margin_mode" in rows  # profile rows still reported


@pytest.mark.parametrize("mode,name", [
    ("NETTING", "NETTING"), (0, "NETTING"), (1, "EXCHANGE"),
    ("ACCOUNT_MARGIN_MODE_RETAIL_NETTING", "NETTING"), ("Exchange", "EXCHANGE"),
])
def test_non_hedging_mode_fails_explicitly(tmp_path, mode, name):
    row = _rows(_suffixed(tmp_path, profile={"account_margin_mode": mode}))["account_margin_mode"]
    assert not row.approved
    assert row.details == f"account is not hedging: {name}; the system needs a hedging account"


@pytest.mark.parametrize("mode", [2, "HEDGING", "hedging", "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING"])
def test_hedging_mode_passes(tmp_path, mode):
    row = _rows(_suffixed(tmp_path, profile={"account_margin_mode": mode}))["account_margin_mode"]
    assert row.approved


@pytest.mark.parametrize("mode", ["weird", 7, -1, ""])
def test_unrecognised_mode_fails(tmp_path, mode):
    row = _rows(_suffixed(tmp_path, profile={"account_margin_mode": mode}))["account_margin_mode"]
    assert not row.approved
    assert row.details == f"unrecognised account_margin_mode={mode}"


def test_broker_symbol_that_differs_from_suffix_fails(tmp_path):
    bridge = _suffixed(tmp_path)
    path = bridge / "market" / "symbols.json"
    data = json.loads(path.read_text())
    data["symbols"]["EURUSD"]["broker_symbol"] = "EURUSD.x"
    path.write_text(json.dumps(data))
    row = _rows(bridge)["symbol_suffix"]
    assert not row.approved
    assert "EURUSD->EURUSD.x (expected EURUSDc)" in row.details


def test_normalise_margin_mode():
    assert normalise_margin_mode(True) is None
    assert normalise_margin_mode(None) is None
    assert normalise_margin_mode(2) == "HEDGING"
