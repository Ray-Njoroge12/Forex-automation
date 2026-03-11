from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from core.env_loader import load_runtime_env
from core.credentials import load_mt5_credentials_from_env

DB_PATH = Path("database/trading_state.db")


def main() -> int:
    load_runtime_env()
    import MetaTrader5 as mt5  # type: ignore

    creds = load_mt5_credentials_from_env()
    if not mt5.initialize(login=creds.login, password=creds.password, server=creds.server):
        print("INIT_FAIL", mt5.last_error(), flush=True)
        return 2

    try:
        last_snapshot = None
        while True:
            positions = mt5.positions_get() or []
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = []
            for pos in positions:
                row = conn.execute(
                    """
                    select trade_id, status, reason_code, open_time, close_time
                    from trades
                    where evidence_stream='runtime_mt5_core_srs'
                      and (position_ticket=? or trade_ticket=?)
                    order by id desc limit 1
                    """,
                    (int(pos.ticket), int(pos.ticket)),
                ).fetchone()
                rows.append({
                    "ticket": int(pos.ticket),
                    "symbol": pos.symbol,
                    "dir": "BUY" if int(pos.type) == int(mt5.POSITION_TYPE_BUY) else "SELL",
                    "volume": float(pos.volume),
                    "entry": float(pos.price_open),
                    "current": float(pos.price_current),
                    "sl": float(pos.sl or 0.0),
                    "tp": float(pos.tp or 0.0),
                    "profit": float(pos.profit),
                    "trade_id": None if row is None else row["trade_id"],
                    "db_status": None if row is None else row["status"],
                })
            conn.close()
            snapshot = tuple((r["ticket"], r["current"], r["sl"], r["tp"], round(r["profit"], 2), r["db_status"]) for r in rows)
            if snapshot != last_snapshot:
                print("WATCH", rows, flush=True)
                last_snapshot = snapshot
            if not rows:
                print("WATCH []", flush=True)
                return 0
            time.sleep(10)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

