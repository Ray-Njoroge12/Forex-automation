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


_MARGIN_MODES = {0: "NETTING", 1: "EXCHANGE", 2: "HEDGING"}


@dataclass(frozen=True)
class Check:
    check_id: str
    approved: bool
    details: str


def normalise_margin_mode(value: object) -> str | None:
    """NETTING, EXCHANGE or HEDGING for MT5 enum ints 0/1/2 or a descriptive string, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _MARGIN_MODES.get(value)
    if isinstance(value, str):
        text = value.upper()
        for needle, name in (("HEDG", "HEDGING"), ("NETT", "NETTING"), ("EXCHANGE", "EXCHANGE")):
            if needle in text:
                return name
    return None


def _profile_checks(conn: FileBridgeConnection, bridge_dir: Path) -> list[Check]:
    """Rows for the optional EA profile fields; only fields the EA exported produce a row."""
    try:
        profile = conn.get_ea_profile()
    except FileBridgeDataError:
        return []  # the ea_status row already reports the unusable status file
    checks: list[Check] = []
    if profile["symbols_ok"] is not None:
        if profile["symbols_ok"]:
            checks.append(Check("symbols_ok", True, "symbols_ok=true"))
        else:
            listed = ", ".join(profile["symbol_errors"] or []) or "unspecified"
            checks.append(Check(
                "symbols_ok", False,
                f"FILE_BRIDGE_SYMBOLS_NOT_OK: EA could not resolve broker symbols "
                f"(suffix={profile['symbol_suffix'] or ''!r}): {listed}",
            ))
    suffix = profile["symbol_suffix"]
    if suffix is not None:
        mapped: list[str] = []
        wrong: list[str] = []
        try:
            specs = conn._read_json(bridge_dir / "market" / "symbols.json").get("symbols", {})
        except (FileBridgeDataError, AttributeError):
            specs = {}
        for canonical, spec in (specs.items() if isinstance(specs, dict) else ()):
            broker = spec.get("broker_symbol") if isinstance(spec, dict) else None
            if broker is None:
                continue
            mapped.append(f"{canonical}->{broker}")
            if broker != canonical + suffix:
                wrong.append(f"{canonical}->{broker} (expected {canonical + suffix})")
        if wrong:
            checks.append(Check("symbol_suffix", False, f"suffix={suffix!r} broker_symbol mismatch: " + ", ".join(wrong)))
        else:
            checks.append(Check("symbol_suffix", True, f"suffix={suffix!r} " + (", ".join(mapped) or "no broker_symbol mapping exported")))
    if profile["account_margin_mode"] is not None:
        raw_mode = profile["account_margin_mode"]
        mode = normalise_margin_mode(raw_mode)
        if mode is None:
            checks.append(Check("account_margin_mode", False, f"unrecognised account_margin_mode={raw_mode}"))
        elif mode != "HEDGING":
            checks.append(Check("account_margin_mode", False, f"account is not hedging: {mode}; the system needs a hedging account"))
        else:
            checks.append(Check("account_margin_mode", True, f"account_margin_mode={mode}"))
    shown = [f"{key}={profile[key]}" for key in ("account_currency", "max_spread_pips_input", "live_guard")
             if profile[key] is not None]
    if shown:
        checks.append(Check("ea_profile", True, " ".join(shown)))
    return checks


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

    checks.extend(_profile_checks(conn, bridge_dir))

    snapshot = conn.get_account_snapshot() or {}
    scale = snapshot.get("unit_scale", 1.0) if "error" not in snapshot else 1.0
    unit_note = (
        f" currency={snapshot.get('currency')} unit_scale={scale} (values in USD)" if scale != 1.0 else ""
    )
    checks.append(Check(
        "account_snapshot",
        "error" not in snapshot,
        snapshot.get("error", {}).get("message", "") if "error" in snapshot
        else f"balance={snapshot.get('balance')} equity={snapshot.get('equity')} open_positions={snapshot.get('open_positions_count')}"
        + unit_note,
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
        if contract is not None:
            tick_note = f"tick_value={contract.tick_value}"
            if scale != 1.0:
                tick_note = f"tick_value_usd={contract.tick_value} tick_value_raw={round(contract.tick_value / scale, 8)}"
            spec_details = f"min_lot={contract.min_lot} step={contract.lot_step} {tick_note}"
        else:
            spec_details = str(conn.last_error)
        checks.append(Check(f"symbol_spec_{symbol}", contract is not None, spec_details))

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
