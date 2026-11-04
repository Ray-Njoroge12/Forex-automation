"""Fixture bridges for offline tests and tooling (never the live terminal folder).

``build_full_usd_bridge`` writes a complete, fresh set of EA exports for all six
symbols. ``convert_bridge_to_usc`` rewrites a copy of it the way a cent (USC)
account reports money: every money field and deal volume in cents.

CLI (timestamps are fresh, so run a checker right after generating):
``python -m tests.bridge_fixtures --kind usd|usc --out DIR``
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_file_bridge_connection import SYMBOLS, _iso, _write, build_bridge

USC_FACTOR = 100.0
USC_CONTRACT_SIZE = 1000

_QUOTES = {
    "EURUSD": (1.10000, 1.10012),
    "GBPUSD": (1.30000, 1.30014),
    "USDJPY": (150.000, 150.012),
    "AUDUSD": (0.66000, 0.66012),
    "USDCAD": (1.36000, 1.36014),
    "USDCHF": (0.88000, 0.88014),
}


def _symbol_spec(symbol: str) -> dict:
    jpy = symbol.endswith("JPY")
    point = 0.001 if jpy else 0.00001
    return {
        "digits": 3 if jpy else 5, "point": point, "tick_size": point, "tick_value": 1.0,
        "contract_size": 100000, "volume_min": 0.01, "volume_step": 0.01, "volume_max": 500,
        "stops_level": 0, "freeze_level": 0, "filling_mode": 1, "trade_mode": 4,
        "swap_long": -1, "swap_short": 0.5,
    }


def build_full_usd_bridge(
    root: Path,
    now: datetime,
    bar_count: int = 400,
    open_tickets: tuple[int, ...] = (111,),
) -> Path:
    """USD bridge with bars, quotes and symbol specs for all six symbols and an empty calendar."""
    root = build_bridge(Path(root), now, open_tickets=open_tickets, bar_count=bar_count)
    _write(root / "feedback" / "market_quotes.json", {
        "schema_version": 1, "timestamp": _iso(now - timedelta(seconds=2)),
        "account_is_demo": True, "account_authenticated": True, "terminal_connected": True,
        "quotes": [
            {"symbol": symbol, "bid": bid, "ask": ask, "tick_age_seconds": 1}
            for symbol, (bid, ask) in _QUOTES.items()
        ],
    })
    _write(root / "market" / "symbols.json", {
        "schema_version": 1, "generated_at": _iso(now - timedelta(seconds=20)),
        "symbols": {symbol: _symbol_spec(symbol) for symbol in SYMBOLS},
    })
    _write(root / "market" / "calendar.json", [])
    return root


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def convert_bridge_to_usc(src: Path, dst: Path, balance_usd: float | None = None) -> Path:
    """Copy *src* to *dst* and express every money value in USC (cents)."""
    src, dst = Path(src), Path(dst)
    shutil.copytree(src, dst, dirs_exist_ok=True)

    snapshot_path = dst / "feedback" / "account_snapshot.json"
    snapshot = _load(snapshot_path)
    snapshot["currency"] = "USC"
    for key in ("balance", "equity", "margin_free", "floating_pnl"):
        snapshot[key] = round(float(snapshot[key]) * USC_FACTOR, 8)
    if balance_usd is not None:
        cents = round(float(balance_usd) * USC_FACTOR, 8)
        snapshot["balance"] = cents
        snapshot["equity"] = cents
        snapshot["margin_free"] = cents
    _write(snapshot_path, snapshot)

    deals_path = dst / "market" / "deals.json"
    if deals_path.exists():
        deals = _load(deals_path)
        for row in deals.get("deals", []):
            for key in ("profit", "commission", "swap", "fee", "volume"):
                if isinstance(row.get(key), (int, float)):
                    row[key] = round(float(row[key]) * USC_FACTOR, 8)
        _write(deals_path, deals)

    symbols_path = dst / "market" / "symbols.json"
    if symbols_path.exists():
        symbols = _load(symbols_path)
        # tick_value stays numerically unchanged: cents (x100) and the 100x smaller
        # contract (1,000 instead of 100,000) cancel.
        for spec in symbols.get("symbols", {}).values():
            spec["contract_size"] = USC_CONTRACT_SIZE
        _write(symbols_path, symbols)

    for exit_path in sorted((dst / "exits").glob("exit_*.json")):
        exit_payload = _load(exit_path)
        if isinstance(exit_payload.get("profit_loss"), (int, float)):
            exit_payload["profit_loss"] = round(float(exit_payload["profit_loss"]) * USC_FACTOR, 8)
            _write(exit_path, exit_payload)
    return dst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a fixture bridge with fresh timestamps.")
    parser.add_argument("--kind", choices=("usd", "usc"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.kind == "usd":
        build_full_usd_bridge(args.out, now)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            usd = build_full_usd_bridge(Path(tmp) / "usd", now)
            convert_bridge_to_usc(usd, args.out)
    print(f"wrote {args.kind} bridge to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
