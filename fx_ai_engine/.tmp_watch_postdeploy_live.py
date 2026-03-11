from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from core.credentials import load_mt5_credentials_from_env
from core.env_loader import load_runtime_env

DB_PATH = Path("database/trading_state.db")
STREAM = "runtime_mt5_core_srs"
ACCOUNT_SCOPE = "mt5:YWO-Trade:21374"
POLL_SECONDS = 10


def _db_open_rows() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            select trade_id, symbol, status, position_ticket, trade_ticket
            from trades
            where evidence_stream=? and account_scope=? and status='EXECUTED_OPEN'
            order by id desc
            """,
            (STREAM, ACCOUNT_SCOPE),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _fmt_pos(pos, mt5) -> dict:
    return {
        "ticket": int(pos.ticket),
        "symbol": pos.symbol,
        "dir": "BUY" if int(pos.type) == int(mt5.POSITION_TYPE_BUY) else "SELL",
        "entry": float(pos.price_open),
        "current": float(pos.price_current),
        "sl": float(pos.sl or 0.0),
        "tp": float(pos.tp or 0.0),
        "profit": round(float(pos.profit), 2),
        "volume": float(pos.volume),
    }


def _notify(message: str) -> None:
    print(f"<augment-user-message>{message}</augment-user-message>", flush=True)


def main() -> int:
    load_runtime_env()
    import MetaTrader5 as mt5  # type: ignore

    creds = load_mt5_credentials_from_env()
    if not mt5.initialize(login=creds.login, password=creds.password, server=creds.server):
        print("INIT_FAIL", mt5.last_error(), flush=True)
        return 2

    try:
        last_positions: dict[int, dict] = {}
        last_db_sig: tuple[tuple[str, int | None], ...] | None = None
        boot_db = _db_open_rows()
        print({"monitor": "started", "positions": [], "db_open": boot_db}, flush=True)
        while True:
            positions = {int(p.ticket): _fmt_pos(p, mt5) for p in (mt5.positions_get() or [])}
            db_open = _db_open_rows()
            db_sig = tuple(sorted((str(r["trade_id"]), int(r["position_ticket"] or 0)) for r in db_open))

            opened = sorted(set(positions) - set(last_positions))
            closed = sorted(set(last_positions) - set(positions))
            shared = sorted(set(positions) & set(last_positions))

            for ticket in opened:
                p = positions[ticket]
                _notify(f"Live trade opened: {p['symbol']} {p['dir']} ticket={ticket} entry={p['entry']} sl={p['sl']} tp={p['tp']} profit={p['profit']}")
            for ticket in shared:
                old = last_positions[ticket]
                new = positions[ticket]
                if old["sl"] != new["sl"] or old["tp"] != new["tp"]:
                    _notify(
                        f"Trade management update: ticket={ticket} symbol={new['symbol']} sl {old['sl']} -> {new['sl']}, tp {old['tp']} -> {new['tp']}, profit={new['profit']}"
                    )
            for ticket in closed:
                old = last_positions[ticket]
                _notify(
                    f"Live trade closed: ticket={ticket} symbol={old['symbol']} last_seen_profit={old['profit']} sl={old['sl']} tp={old['tp']}"
                )

            if last_db_sig is not None and db_sig != last_db_sig:
                _notify(f"DB open-trade ledger changed: {db_open}")

            if positions or db_open:
                print({"positions": list(positions.values()), "db_open": db_open}, flush=True)

            if len(db_open) != len(positions):
                print({"warning": "broker_db_mismatch", "positions": list(positions), "db_open": db_open}, flush=True)

            last_positions = positions
            last_db_sig = db_sig
            time.sleep(POLL_SECONDS)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

