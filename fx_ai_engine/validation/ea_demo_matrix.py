"""Generate controlled EA acceptance-test payloads for a demo terminal.

The generated files are evidence fixtures, not signals for a funded account.
Copy only one case at a time into the EA pending-signals folder and reconcile
the observed feedback/log output with the matrix manifest.

Signals expire 120 seconds after generation, so generate the files right before
copying a case into pending_signals. Cases added for the cent-account EA build
(2026.11.04-2) carry ``required_terminal_setup`` in the manifest: the account,
the EA inputs, any file to create, and the case that must run first. Nothing here
runs a case or touches a terminal.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config_microcapital import MODE_CONFIGS, get_policy_config
from core.policy.envelope import attach_execution_envelope, canonical_policy_hash


@dataclass(frozen=True)
class DemoCase:
    case_id: str
    expected_result: str
    operator_setup: str
    payload: dict[str, Any]
    terminal_setup: dict[str, Any] = field(default_factory=dict)


def _base_payload(case_id: str) -> dict[str, Any]:
    return {
        "trade_id": f"DEMO_MATRIX_{case_id}",
        "symbol": "EURUSD",
        "direction": "BUY",
        "risk_percent": 0.001,
        "stop_pips": 20.0,
        "take_profit_pips": 44.0,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "order_type": "MARKET",
    }


def build_demo_matrix(mode_id: str = "core_srs") -> tuple[dict[str, str], list[DemoCase]]:
    policy = get_policy_config(mode_id=mode_id)
    now = datetime.now(timezone.utc)

    valid = attach_execution_envelope(
        _base_payload("VALID"), policy=policy, reservation_id="RSV_DEMO_VALID", now_utc=now
    )
    expired = attach_execution_envelope(
        _base_payload("EXPIRED"),
        policy=policy,
        reservation_id="RSV_DEMO_EXPIRED",
        now_utc=now - timedelta(minutes=5),
        ttl_seconds=1,
    )
    hash_mismatch = attach_execution_envelope(
        _base_payload("POLICY_HASH"), policy=policy, reservation_id="RSV_DEMO_POLICY_HASH", now_utc=now
    )
    hash_mismatch["policy_hash"] = "0" * 64
    low_spread_limit = attach_execution_envelope(
        _base_payload("SPREAD"), policy=policy, reservation_id="RSV_DEMO_SPREAD", now_utc=now
    )
    low_spread_limit["max_spread_pips"] = 0.0001
    malformed = _base_payload("MALFORMED")
    malformed["schema_version"] = 999

    pin = {"mode_id": str(policy["MODE_ID"]), "policy_hash": canonical_policy_hash(policy)}
    return pin, [
        DemoCase(
            "valid",
            "Order is eligible for normal broker execution; record execution feedback or broker rejection.",
            "Use a disposable demo account with no open EURUSD position and sufficient margin.",
            valid,
        ),
        DemoCase(
            "expired",
            "REJECTED_EXPIRED feedback; no order; EA log reports expired envelope.",
            "Copy after its expiry time.",
            expired,
        ),
        DemoCase(
            "policy_hash_mismatch",
            "REJECTED_POLICY_HASH feedback; no order.",
            "Set EA ExpectedPolicyHash to the policy hash in manifest.json.",
            hash_mismatch,
        ),
        DemoCase(
            "spread_limit",
            "REJECTED_SPREAD feedback; no order when the live EURUSD spread exceeds 0.0001 pips.",
            "Run only while a non-zero EURUSD quote is available.",
            low_spread_limit,
        ),
        DemoCase(
            "malformed_schema",
            "No order; EA log reports unsupported schema version.",
            "No special setup required.",
            malformed,
        ),
        DemoCase(
            "position_limit",
            "REJECTED_POSITION_LIMIT feedback; no additional order.",
            "Before copying valid.json under a new trade id, open the configured number of demo positions/orders.",
            valid | {"trade_id": "DEMO_MATRIX_POSITION_LIMIT", "reservation_id": "RSV_DEMO_POSITION_LIMIT"},
        ),
        DemoCase(
            "restart_duplicate",
            "An existing submission marker prevents another order; retain marker and reconcile broker history.",
            "Use one disposable demo EA; retain the signal and restart after the marker is created. Verify orders and deals before cleanup.",
            valid | {"trade_id": "DEMO_MATRIX_RESTART", "reservation_id": "RSV_DEMO_RESTART"},
        ),
    ]


def build_cent_guard_cases(mode_id: str = "core_srs") -> list[DemoCase]:
    """Cases for the symbol suffix (C1), account mode (C4) and live guard (C5) of EA 2026.11.04-2.

    Every payload uses the canonical symbol EURUSD; the EA adds ``SymbolSuffix``
    itself. Trade ids are unique ``DEMO_MATRIX_*`` ids so the runner can move their
    feedback out of the bridge.
    """
    policy = get_policy_config(mode_id=mode_id)
    now = datetime.now(timezone.utc)

    def signal(case_id: str) -> dict[str, Any]:
        return attach_execution_envelope(
            _base_payload(case_id), policy=policy, reservation_id=f"RSV_DEMO_{case_id}", now_utc=now
        )

    lot_formula = "lot = balance x risk_percent / (stop_pips x pip value per lot), stepped down to volume_step (risk_percent 0.001, stop_pips 20)"
    return [
        DemoCase(
            "suffix_resolved",
            "Signal for canonical EURUSD is routed to the suffixed broker symbol: execution feedback EXECUTED "
            "(or a broker rejection such as REJECTED_ORDER_CHECK). market/status.json has symbols_ok=true and "
            "symbol_suffix equal to the input; bars_EURUSD_<TF>.json exist and no bars file name carries the suffix; "
            "symbols.json is keyed by canonical names and each entry has broker_symbol = canonical + suffix.",
            "Attach the EA with SymbolSuffix set to the broker's suffix on a demo whose symbols carry it.",
            signal("SUFFIX_RESOLVED"),
            {
                "account": "Demo account whose six majors carry a suffix (for example Exness Standard Cent demo, suffix 'c'), hedging",
                "ea_inputs": {"SymbolSuffix": "c", "RequireDemoAccount": True},
                "files": [],
                "prerequisite_case": None,
            },
        ),
        DemoCase(
            "symbol_missing",
            "REJECTED_SYMBOL feedback; no order. market/status.json has symbols_ok=false and symbol_errors lists "
            "the unresolved canonical names (all six). EA log: 'Symbol gate reject'.",
            "Attach the EA with a SymbolSuffix no broker symbol has. Restore SymbolSuffix to its normal value afterwards.",
            signal("SYMBOL_MISSING"),
            {
                "account": "Any demo account (the current MetaQuotes-Demo works)",
                "ea_inputs": {"SymbolSuffix": "_NOSUCH", "RequireDemoAccount": True},
                "files": [],
                "prerequisite_case": None,
            },
        ),
        DemoCase(
            "account_mode_netting",
            "REJECTED_ACCOUNT_MODE feedback; no order; the signal file is consumed. market/status.json has "
            "account_margin_mode='netting'. EA log: 'Account mode gate reject'.",
            "Needs a demo account whose margin mode is netting (or exchange). The default demo is hedging and cannot run this case.",
            signal("ACCOUNT_MODE_NETTING"),
            {
                "account": "Netting (or exchange) demo account; confirm account_margin_mode in market/status.json first",
                "ea_inputs": {"RequireDemoAccount": True},
                "files": [],
                "prerequisite_case": None,
            },
        ),
        DemoCase(
            "kill_switch",
            "REJECTED_LIVE_GUARD feedback; no order; EA log reason 'kill switch file present'. market/status.json "
            "live_guard.kill_switch=true. Management of the open position from the prerequisite case continues while "
            "the file exists: the case passes that part only if ea_debug.log shows a management line for that position's "
            "ticket written after the file was created ('BE moved SL', 'Partial close' or 'Trail SL'). If price never "
            "reaches a management trigger, record the management part as inconclusive, not passed. After deleting the "
            "file a new signal with a new trade id can execute again.",
            "First run the valid case and keep its position open (do not close it by hand). Confirm "
            "feedback/management_state_<ticket>.json exists for it and the position has not yet reached its break-even "
            "trigger. Then create an empty file named KILL_SWITCH in the bridge root folder (next to ea_debug.log), copy "
            "this case, and keep watching ea_debug.log until the position hits a trigger or you stop the case.",
            signal("KILL_SWITCH"),
            {
                "account": "Demo account, hedging (the current MetaQuotes-Demo works), with the valid case's position still open and managed",
                "ea_inputs": {"RequireDemoAccount": True},
                "files": [
                    "<RootFolder>\\KILL_SWITCH (any content, created by the operator; delete it afterwards)",
                    "<RootFolder>\\feedback\\management_state_<ticket>.json (written by the EA for the open position; read it, do not edit it)",
                ],
                "prerequisite_case": "valid",
            },
        ),
        DemoCase(
            "live_lot_ceiling",
            "EXECUTED with execution feedback lot_size equal to LiveMaxLotPerOrder stepped down to the lot step and "
            "below the uncapped lot; EA log 'Live lot ceiling applied: <uncapped> -> <capped>'.",
            "Set LiveMaxLotPerOrder below the lot the EA computes for this signal: " + lot_formula + ". "
            "The cap applies on demo because the input is set explicitly. Places a real demo order.",
            signal("LIVE_LOT_CEILING"),
            {
                "account": "Demo account, hedging, no open EURUSD position, enough margin",
                "ea_inputs": {"LiveMaxLotPerOrder": "below the computed lot, for example 0.05", "RequireDemoAccount": True},
                "files": [],
                "prerequisite_case": None,
            },
        ),
        DemoCase(
            "live_lot_below_minimum",
            "REJECTED_LIVE_GUARD feedback; no order; EA log reason 'capped lot ... is below broker minimum'.",
            "Set LiveMaxLotPerOrder smaller than the symbol's volume_min (for example 0.005 when volume_min is 0.01) "
            "and below the computed lot (" + lot_formula + ").",
            signal("LIVE_LOT_BELOW_MINIMUM"),
            {
                "account": "Demo account, hedging",
                "ea_inputs": {"LiveMaxLotPerOrder": "smaller than volume_min, for example 0.005", "RequireDemoAccount": True},
                "files": [],
                "prerequisite_case": None,
            },
        ),
        DemoCase(
            "live_daily_cap",
            "REJECTED_LIVE_GUARD feedback once the UTC day's count reached the cap; no order; EA log reason "
            "'daily order cap reached: 1/1'. feedback/live_orders_day.json shows orders equal to LiveMaxOrdersPerDay "
            "and market/status.json live_guard.orders_today equals it.",
            "Set LiveMaxOrdersPerDay=1, then run the valid case first (it counts as the day's one order), then this case. "
            "The cap applies on demo because the input is set explicitly. Delete or wait out the UTC day before re-running.",
            signal("LIVE_DAILY_CAP"),
            {
                "account": "Demo account, hedging, no open EURUSD position",
                "ea_inputs": {"LiveMaxOrdersPerDay": 1, "RequireDemoAccount": True},
                "files": ["<RootFolder>\\feedback\\live_orders_day.json (written by the EA; read it, do not edit it)"],
                "prerequisite_case": "valid",
            },
        ),
    ]


def write_demo_matrix(output_dir: Path, *, mode_id: str = "core_srs") -> Path:
    target = output_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)
    pin, cases = build_demo_matrix(mode_id)
    cases = cases + build_cent_guard_cases(mode_id)
    for case in cases:
        (target / f"{case.case_id}.json").write_text(
            json.dumps(case.payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    manifest = {
        "purpose": "demo EA acceptance testing only",
        "policy_pin": pin,
        "cases": [
            {
                "case_id": case.case_id,
                "expected_result": case.expected_result,
                "operator_setup": case.operator_setup,
                "payload_file": f"{case.case_id}.json",
                "required_terminal_setup": case.terminal_setup,
            }
            for case in cases
        ],
    }
    manifest_path = target / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate demo-only MT5 EA acceptance-test payloads")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=sorted(MODE_CONFIGS), default="core_srs")
    args = parser.parse_args(argv)
    manifest = write_demo_matrix(args.output_dir, mode_id=args.mode)
    print(f"Generated demo-only EA acceptance matrix: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
