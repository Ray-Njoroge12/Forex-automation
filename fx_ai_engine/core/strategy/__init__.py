"""Strategy plug-ins (stage D, ADR 0004).

Importing this package registers every strategy, so the live engine and the
replay see the same registry. Live runs only ``DEFAULT_STRATEGY_ID``.
"""
from core.strategy.base import (  # noqa: F401
    LIVE_BAR_EXPORT_LIMIT, Strategy, StrategyDecision, TECH_SKIPPED_REGIME_REJECTED, STRATEGY_ERROR,
)
from core.strategy.guard import GuardedStrategy  # noqa: F401
from core.strategy.registry import (  # noqa: F401
    DEFAULT_STRATEGY_ID, StrategyNotLiveEligibleError, available_strategies, build_strategy, canonical_params,
    is_live_eligible, live_block_reason, register_strategy, require_live_eligible, resolve_params,
)

# Strategy modules register themselves on import (order does not matter).
from core.strategy import trend_pullback  # noqa: F401,E402
from core.strategy import london_breakout  # noqa: F401,E402
from core.strategy import h1_channel_breakout  # noqa: F401,E402
from core.strategy import d1_breakout  # noqa: F401,E402
