"""Read-only check of the EA file-bridge exports the engine depends on.

Usage (from fx_ai_engine/):
    python -m validation.file_bridge_check --bridge-dir "/path/to/MQL5/Files/bridge"
    python -m validation.file_bridge_check --json

Without --bridge-dir the BRIDGE_BASE_PATH environment variable (or .env) is used.
Nothing is written to the bridge folder and no order can be placed.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from core.env_loader import load_runtime_env
from core.file_bridge_connection import FileBridgeConnection, FileBridgeDataError

SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF")
TIMEFRAMES = {"M15": 15, "H1": 16385, "H4": 16388, "D1": 16408}
# Bars each consumer requests (RegimeAgent/TechnicalAgent 350; correlation 66 D1).
REQUIRED_BARS = {"M15": 350, "H1": 350, "H4": 350, "D1": 66}


@dataclass(frozen=True)
class Check:
    check_id: str
    approved: bool
    details: str


def run_checks(bridge_dir: Path) -> list[Check]:
    conn = FileBridgeConnection(bridge_dir)
    checks: list[Check] = []

    connected = conn.connect(retry_count=1)
    checks.append(Check(
        "ea_status",
        connected,
        f"login={conn.login} server={conn.server}" if connected else str(conn.last_error),
    ))
    demo = conn.is_demo_account()
    checks.append(Check("demo_account", demo is True, f"is_demo_account={demo}"))
    try:
        status = conn._status()
        flags = {k: status.get(k) for k in ("terminal_trade_allowed", "ea_trade_allowed", "account_trade_allowed")}
        checks.append(Check(
            "trading_enabled",
            all(v is True for v in flags.values()),
            " ".join(f"{k}={v}" for k, v in flags.items())
            + ("" if all(v is True for v in flags.values()) else " (enable the MT5 Algo Trading button and the EA's Allow Algo Trading)"),
        ))
    except FileBridgeDataError as exc:
        checks.append(Check("trading_enabled", False, str(exc)))

    snapshot = conn.get_account_snapshot() or {}
    checks.append(Check(
        "account_snapshot",
        "error" not in snapshot,
        snapshot.get("error", {}).get("message", "") if "error" in snapshot
        else f"balance={snapshot.get('balance')} equity={snapshot.get('equity')} open_positions={snapshot.get('open_positions_count')}",
    ))
    tickets = conn.get_open_position_tickets()
    checks.append(Check("position_tickets", tickets is not None, f"tickets={tickets}" if tickets is not None else str(conn.last_error)))

    for symbol in SYMBOLS:
        for tf_name, tf in TIMEFRAMES.items():
            df = conn.fetch_ohlc_data(symbol, tf, 1000)
            if df.empty:
                checks.append(Check(f"bars_{symbol}_{tf_name}", False, df.attrs.get("error", {}).get("message", "empty")))
                continue
            enough = len(df) >= REQUIRED_BARS[tf_name]
            checks.append(Check(
                f"bars_{symbol}_{tf_name}",
                enough,
                f"rows={len(df)} required={REQUIRED_BARS[tf_name]} last_bar_utc={df.index[-1].isoformat()}",
            ))
        spread = conn.get_live_spread(symbol)
        pip = 0.01 if symbol.endswith("JPY") else 0.0001
        checks.append(Check(
            f"spread_{symbol}",
            spread is not None,
            f"spread_pips={spread / pip:.2f}" if spread is not None else str(conn.last_error),
        ))
        contract = conn.get_symbol_execution_contract(symbol)
        checks.append(Check(
            f"symbol_spec_{symbol}",
            contract is not None,
            f"min_lot={contract.min_lot} step={contract.lot_step} tick_value={contract.tick_value}" if contract else str(conn.last_error),
        ))

    deals = conn._deals()
    checks.append(Check("deal_history", deals is not None, f"deals_30d={len(deals)}" if deals is not None else str(conn.last_error)))

    calendar_path = bridge_dir / "market" / "calendar.json"
    try:
        events = json.loads(calendar_path.read_text(encoding="utf-8"))
        ok = isinstance(events, list)
        checks.append(Check("calendar_export", ok, f"high_impact_events={len(events)} file={calendar_path}"))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(Check("calendar_export", False, f"{type(exc).__name__}: {exc}"))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check EA file-bridge exports (read-only)")
    parser.add_argument("--bridge-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    load_runtime_env()
    bridge_dir = args.bridge_dir or (Path(os.environ["BRIDGE_BASE_PATH"]) if os.getenv("BRIDGE_BASE_PATH") else None)
    if bridge_dir is None:
        parser.error("pass --bridge-dir or set BRIDGE_BASE_PATH")
    try:
        checks = run_checks(bridge_dir)
    except FileBridgeDataError as exc:
        checks = [Check("file_bridge", False, str(exc))]
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for c in checks:
            print(f"{'PASS' if c.approved else 'BLOCK':5} {c.check_id}: {c.details}")
        failed = sum(not c.approved for c in checks)
        print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 0 if all(c.approved for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
