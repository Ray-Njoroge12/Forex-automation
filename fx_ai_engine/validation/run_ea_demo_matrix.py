"""Run EA acceptance cases one at a time against the DEMO terminal and keep evidence.

Usage (from fx_ai_engine/, engine NOT running):
    python -m validation.run_ea_demo_matrix --cases malformed_schema expired policy_hash_mismatch
    python -m validation.run_ea_demo_matrix --cases valid position_limit duplicate --wait-exit

Each case is generated fresh (signals expire after 120 s), written atomically
into pending_signals/, and observed until the EA reports an outcome. Evidence
goes to artifacts/demo-evidence/<UTC date>/<case>/. Feedback, exit and
submission-marker files produced for DEMO_MATRIX_* trades are moved out of the
bridge into the evidence folder so the engine never consumes test artifacts.

Refuses to run unless the EA status reports a connected demo account.
The `valid` case places a real order on the demo account.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config_microcapital import get_policy_config
from core.env_loader import load_runtime_env
from core.policy.envelope import attach_execution_envelope
from validation.ea_demo_matrix import _base_payload

PROJECT_ROOT = Path(__file__).parents[1]
CASE_ORDER = ("malformed_schema", "expired", "policy_hash_mismatch", "spread_limit", "valid", "position_limit", "duplicate")
VALID_TRADE_ID = "DEMO_MATRIX_VALID"


def _bridge() -> Path:
    load_runtime_env()
    raw = os.getenv("BRIDGE_BASE_PATH")
    if not raw:
        raise SystemExit("BRIDGE_BASE_PATH is not set (see fx_ai_engine/.env)")
    return Path(raw)


def _status(bridge: Path) -> dict:
    return json.loads((bridge / "market" / "status.json").read_text(encoding="utf-8"))


def _quotes(bridge: Path) -> dict:
    data = json.loads((bridge / "feedback" / "market_quotes.json").read_text(encoding="utf-8"))
    return {q["symbol"]: q for q in data.get("quotes", [])}


def _snapshot(bridge: Path) -> dict:
    return json.loads((bridge / "feedback" / "account_snapshot.json").read_text(encoding="utf-8"))


def _payload(case: str, policy: dict, run_tag: str, symbol: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    if case == "malformed_schema":
        payload = _base_payload("MALFORMED")
        payload["schema_version"] = 999
        return payload
    if case == "expired":
        return attach_execution_envelope(_base_payload("EXPIRED"), policy=policy,
                                         reservation_id="RSV_DEMO_EXPIRED", now_utc=now - timedelta(minutes=5), ttl_seconds=1)
    base = _base_payload("VALID" if case in {"valid", "duplicate"} else case.upper())
    if case in {"valid", "duplicate", "position_limit"}:
        # Tight stop/target (still 2.2R) so the demo position closes by itself.
        base.update({"stop_pips": 3.0, "take_profit_pips": 6.6, "tp_mode": "FIXED"})
    payload = attach_execution_envelope(base, policy=policy, reservation_id=f"RSV_DEMO_{case.upper()}_{run_tag}", now_utc=now)
    if case == "policy_hash_mismatch":
        payload["policy_hash"] = "0" * 64
    elif case == "spread_limit":
        payload["max_spread_pips"] = 0.0001
        if symbol:
            payload["symbol"] = symbol
    elif case == "position_limit":
        payload["trade_id"] = "DEMO_MATRIX_POSITION_LIMIT"
        payload["max_positions"] = 1
    return payload


def _deliver(bridge: Path, payload: dict) -> Path:
    pending = bridge / "pending_signals"
    target = pending / f"{payload['trade_id']}.json"
    tmp = pending / f".{payload['trade_id']}.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(target)
    return target


def _log_size(bridge: Path) -> int:
    path = bridge / "ea_debug.log"
    return path.stat().st_size if path.exists() else 0


def _log_since(bridge: Path, offset: int) -> list[str]:
    path = bridge / "ea_debug.log"
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("latin-1").splitlines()


def _collect(bridge: Path, trade_id: str, evidence: Path, *, move: bool) -> list[str]:
    """Move test feedback out of the bridge (the engine must never consume it).

    ``move=False`` keeps only the submission marker in place so the duplicate
    case can prove the EA refuses to resend; feedback is always moved.
    """
    collected = []
    feedback = bridge / "feedback" / f"execution_{trade_id}.json"
    marker = bridge / "feedback" / f"submission_{trade_id}.lock"
    if feedback.exists():
        shutil.move(str(feedback), evidence / feedback.name)
        collected.append(feedback.name)
    if marker.exists():
        (shutil.move if move else shutil.copy2)(str(marker), evidence / marker.name)
        collected.append(marker.name)
    return collected


def run_case(bridge: Path, case: str, policy: dict, evidence_root: Path, run_tag: str, timeout: float,
             symbol: str | None = None) -> dict:
    evidence = evidence_root / case
    evidence.mkdir(parents=True, exist_ok=True)
    payload = _payload(case, policy, run_tag, symbol)
    trade_id = payload["trade_id"]
    before = _snapshot(bridge)
    log_offset = _log_size(bridge)
    (evidence / "payload.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    signal = _deliver(bridge, payload)

    deadline = time.time() + timeout
    feedback_path = bridge / "feedback" / f"execution_{trade_id}.json"
    outcome = "TIMEOUT"
    feedback = None
    while time.time() < deadline:
        time.sleep(1)
        lines = _log_since(bridge, log_offset)
        if case == "duplicate" and any("Submission marker exists" in line for line in lines):
            outcome = "HELD_FOR_RECONCILIATION"
            break
        if feedback_path.exists() and not signal.exists():
            try:
                feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
                outcome = feedback.get("status", "UNKNOWN")
                break
            except json.JSONDecodeError:
                continue
        if not signal.exists() and not feedback_path.exists() and case in {"malformed_schema"}:
            outcome = "DELETED_NO_FEEDBACK"
            break
    time.sleep(2)
    lines = _log_since(bridge, log_offset)
    (evidence / "ea_debug.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if case == "duplicate" and signal.exists():
        signal.unlink()  # leave no retained duplicate in the live bridge
    after = _snapshot(bridge)
    result = {
        "case": case,
        "trade_id": trade_id,
        "outcome": outcome,
        "feedback": feedback,
        "positions_before": before.get("open_positions_count"),
        "positions_after": after.get("open_positions_count"),
        "signal_file_remaining": signal.exists(),
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    move = case not in {"valid"}  # keep the valid trade's marker/feedback until its exit is captured
    result["artifacts"] = _collect(bridge, trade_id, evidence, move=move)
    (evidence / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def wait_for_valid_exit(bridge: Path, evidence_root: Path, timeout: float) -> dict:
    evidence = evidence_root / "valid"
    feedback = json.loads((evidence / f"execution_{VALID_TRADE_ID}.json").read_text(encoding="utf-8"))
    ticket = int(feedback.get("position_ticket") or 0)
    if feedback.get("status") != "EXECUTED" or ticket <= 0:
        _collect(bridge, VALID_TRADE_ID, evidence, move=True)
        return {"position_ticket": ticket, "exit_observed": False, "skipped": f"valid case was {feedback.get('status')}"}
    exit_path = bridge / "exits" / f"exit_{ticket}.json"
    deadline = time.time() + timeout
    while time.time() < deadline and not exit_path.exists():
        time.sleep(5)
    result = {"position_ticket": ticket, "exit_observed": exit_path.exists(), "observed_at_utc": datetime.now(timezone.utc).isoformat()}
    if exit_path.exists():
        time.sleep(1)
        result["exit"] = json.loads(exit_path.read_text(encoding="utf-8"))
        shutil.move(str(exit_path), evidence / exit_path.name)
        _collect(bridge, VALID_TRADE_ID, evidence, move=True)
        for state in (bridge / "feedback").glob(f"management_state_{ticket}.json"):
            shutil.move(str(state), evidence / state.name)
    (evidence / "exit_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run EA demo acceptance cases with evidence capture")
    parser.add_argument("--cases", nargs="+", choices=CASE_ORDER, default=list(CASE_ORDER))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--wait-exit", action="store_true", help="after 'valid', wait for its TP/SL exit")
    parser.add_argument("--exit-timeout", type=float, default=1800.0)
    args = parser.parse_args(argv)

    bridge = _bridge()
    status = _status(bridge)
    if status.get("account_is_demo") is not True or status.get("terminal_connected") is not True:
        raise SystemExit("Refusing: EA status does not report a connected demo account")
    policy = get_policy_config(mode_id="core_srs")
    stamp = datetime.now(timezone.utc)
    run_tag = stamp.strftime("%H%M%S")
    evidence_root = PROJECT_ROOT / "artifacts" / "demo-evidence" / stamp.strftime("%Y-%m-%d") / f"run_{run_tag}"
    print(f"Demo account {status.get('login')} on {status.get('server')}; evidence -> {evidence_root}")

    for case in [c for c in CASE_ORDER if c in args.cases]:
        spread_symbol = None
        if case == "spread_limit":
            quotes = _quotes(bridge)
            widest = max(quotes.values(), key=lambda q: (q["ask"] - q["bid"]) / (0.01 if q["symbol"].endswith("JPY") else 0.0001), default=None)
            if not widest or widest["ask"] - widest["bid"] <= 0:
                print("SKIP spread_limit: every spread is zero right now, the case could execute")
                continue
            spread_symbol = widest["symbol"]
        if case in {"position_limit", "duplicate"} and int(_snapshot(bridge).get("open_positions_count", 0)) < 1:
            print(f"SKIP {case}: needs the 'valid' demo position to be open")
            continue
        result = run_case(bridge, case, policy, evidence_root, run_tag, args.timeout, spread_symbol)
        print(f"{case:22} outcome={result['outcome']:28} positions {result['positions_before']}->{result['positions_after']}")
    if "valid" in args.cases and not args.wait_exit:
        print("NOTE: valid-case feedback is still in the bridge; rerun with --wait-exit before starting the engine")
    if args.wait_exit and "valid" in args.cases:
        print("Waiting for the valid position to exit ...")
        exit_result = wait_for_valid_exit(bridge, evidence_root, args.exit_timeout)
        print(json.dumps(exit_result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
