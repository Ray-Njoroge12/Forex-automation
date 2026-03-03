from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Differentials with abs value above this (in percent) are considered significant.
MACRO_THRESHOLD_PERCENT = 0.5


def load_rate_differentials(path: str) -> dict[str, float]:
    """Load rate differential JSON.  Returns empty dict on any failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw: dict = json.load(fh)
        return {k: float(v) for k, v in raw.items() if not k.startswith("_")}
    except FileNotFoundError:
        logger.warning("rate_differentials.json not found at %s — macro filter inactive", path)
        return {}
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse rate_differentials.json: %s — macro filter inactive", exc)
        return {}


def is_macro_aligned(
    symbol: str,
    direction: str,
    differentials: dict[str, float],
) -> bool:
    """Return False only when macro strongly opposes the trade direction.

    Convention: differential = base_rate - quote_rate (percent).
      Positive → base currency earns more → macro bias is BUY (the base).
      Negative → quote currency earns more → macro bias is SELL (the base).

    Soft filter: misalignment is only flagged when abs(differential) > MACRO_THRESHOLD_PERCENT.
    Missing symbols return True (neutral — no filter applied).
    """
    diff = differentials.get(symbol)
    if diff is None:
        return True  # no data → neutral

    if abs(diff) <= MACRO_THRESHOLD_PERCENT:
        return True  # weak differential → not significant enough to flag

    if diff > MACRO_THRESHOLD_PERCENT and direction == "SELL":
        return False  # macro favours BUY, signal says SELL

    if diff < -MACRO_THRESHOLD_PERCENT and direction == "BUY":
        return False  # macro favours SELL, signal says BUY

    return True
