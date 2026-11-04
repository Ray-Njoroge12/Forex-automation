"""The cent-account EA cases in the demo matrix: ids, expected statuses, payloads, manifest.

Nothing here runs a case. The cases need a terminal; see the manifest's required_terminal_setup.
"""
from __future__ import annotations

import json

from validation.ea_demo_matrix import build_cent_guard_cases, build_demo_matrix, write_demo_matrix

ORIGINAL_IDS = {
    "valid", "expired", "policy_hash_mismatch", "spread_limit", "malformed_schema", "position_limit", "restart_duplicate",
}
NEW_STATUS = {
    "suffix_resolved": "EXECUTED",
    "symbol_missing": "REJECTED_SYMBOL",
    "account_mode_netting": "REJECTED_ACCOUNT_MODE",
    "kill_switch": "REJECTED_LIVE_GUARD",
    "live_lot_ceiling": "EXECUTED",
    "live_lot_below_minimum": "REJECTED_LIVE_GUARD",
    "live_daily_cap": "REJECTED_LIVE_GUARD",
}
CANONICAL = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"}


def test_original_matrix_is_unchanged():
    _, cases = build_demo_matrix("core_srs")
    assert {case.case_id for case in cases} == ORIGINAL_IDS
    assert all(case.terminal_setup == {} for case in cases)


def test_new_cases_exist_with_the_expected_statuses():
    cases = {case.case_id: case for case in build_cent_guard_cases("core_srs")}
    assert set(cases) == set(NEW_STATUS)
    for case_id, status in NEW_STATUS.items():
        assert status in cases[case_id].expected_result, case_id


def test_new_payloads_are_canonical_pinned_and_unique():
    pin, original = build_demo_matrix("core_srs")
    new = build_cent_guard_cases("core_srs")
    trade_ids = [case.payload["trade_id"] for case in original + new]
    assert len(trade_ids) == len(set(trade_ids)) == 14
    for case in new:
        assert case.payload["symbol"] in CANONICAL
        assert case.payload["trade_id"].startswith("DEMO_MATRIX_")
        assert case.payload["policy_hash"] == pin["policy_hash"]
        assert case.payload["schema_version"] == 2
        assert case.payload["reservation_id"]


def test_new_cases_state_their_terminal_setup():
    for case in build_cent_guard_cases("core_srs"):
        setup = case.terminal_setup
        assert setup, case.case_id
        assert set(setup) == {"account", "ea_inputs", "files", "prerequisite_case"}, case.case_id
        assert setup["account"] and isinstance(setup["ea_inputs"], dict)


def test_setup_matches_the_behaviour_each_case_tests():
    cases = {case.case_id: case.terminal_setup for case in build_cent_guard_cases("core_srs")}
    assert cases["suffix_resolved"]["ea_inputs"]["SymbolSuffix"]
    assert cases["symbol_missing"]["ea_inputs"]["SymbolSuffix"]
    assert "etting" in cases["account_mode_netting"]["account"]
    assert any("KILL_SWITCH" in item for item in cases["kill_switch"]["files"])
    assert "LiveMaxLotPerOrder" in cases["live_lot_ceiling"]["ea_inputs"]
    assert "LiveMaxLotPerOrder" in cases["live_lot_below_minimum"]["ea_inputs"]
    assert cases["live_daily_cap"]["ea_inputs"]["LiveMaxOrdersPerDay"] == 1
    assert cases["live_daily_cap"]["prerequisite_case"] == "valid"


def test_manifest_lists_all_fourteen_cases_with_setup(tmp_path):
    manifest_path = write_demo_matrix(tmp_path / "matrix")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ids = {entry["case_id"] for entry in manifest["cases"]}
    assert ids == ORIGINAL_IDS | set(NEW_STATUS)
    for entry in manifest["cases"]:
        assert (tmp_path / "matrix" / entry["payload_file"]).is_file()
        assert "required_terminal_setup" in entry
        if entry["case_id"] in NEW_STATUS:
            assert entry["required_terminal_setup"], entry["case_id"]
        else:
            assert entry["required_terminal_setup"] == {}
    assert len(list((tmp_path / "matrix").glob("*.json"))) == 15  # 14 payloads + manifest
