"""Generate controlled EA acceptance-test payloads for a demo terminal.

The generated files are evidence fixtures, not signals for a funded account.
Copy only one case at a time into the EA pending-signals folder and reconcile
the observed feedback/log output with the matrix manifest.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
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
            "No order; EA log reports expired envelope.",
            "Copy after its expiry time; do not rely on a feedback file for this parser-level rejection.",
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


def write_demo_matrix(output_dir: Path, *, mode_id: str = "core_srs") -> Path:
    target = output_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)
    pin, cases = build_demo_matrix(mode_id)
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
