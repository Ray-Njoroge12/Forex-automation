from __future__ import annotations

import logging
import sys

from core.credentials import CredentialsError, load_mt5_credentials_from_env
from core.logging_utils import configure_logging
from core.mt5_bridge import MT5Connection, mt5


def run_test() -> int:
    configure_logging()
    logger = logging.getLogger("fx_ai_engine.test_bridge")

    try:
        creds = load_mt5_credentials_from_env()
    except CredentialsError as exc:
        logger.error("Credential loading failed: %s", exc)
        return 1

    bridge = MT5Connection(login=creds.login, password=creds.password, server=creds.server)

    if not bridge.connect():
        logger.error("Bridge connect failed. last_error=%s", bridge.last_error)
        return 2

    snapshot = bridge.get_account_snapshot()
    print("\n--- Account Snapshot ---")
    print(snapshot)

    if snapshot and not snapshot.get("error") and mt5 is not None:
        print("\n--- Live Data Test (EURUSD M15, last 5 bars) ---")
        df = bridge.fetch_ohlc_data("EURUSD", mt5.TIMEFRAME_M15, num_candles=5)
        if df.empty:
            logger.error("OHLC fetch failed. error=%s", df.attrs.get("error"))
        else:
            print(df[["open", "high", "low", "close"]])

        spread = bridge.get_live_spread("EURUSD")
        if spread is None:
            logger.error("Spread fetch failed. last_error=%s", bridge.last_error)
        else:
            print(f"\nLive EURUSD Spread: {spread:.5f}")

    bridge.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(run_test())
