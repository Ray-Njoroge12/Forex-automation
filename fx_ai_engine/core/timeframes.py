from __future__ import annotations

import os

mt5 = None
try:
    if os.getenv("USE_MT5_MOCK") == "1":
        try:
            import MMetaTrader5 as mt5  # type: ignore
        except Exception:
            from core import mt5_mock as mt5  # type: ignore
    else:
        try:
            import mt5_wrapper as mt5  # type: ignore
        except Exception:
            import MetaTrader5 as mt5  # type: ignore
except Exception:  # pragma: no cover
    mt5 = None

# Fallback values are used only when MetaTrader5 is unavailable in local runtime/tests.
TIMEFRAME_M15 = mt5.TIMEFRAME_M15 if mt5 is not None else 15
TIMEFRAME_H1 = mt5.TIMEFRAME_H1 if mt5 is not None else 16385
