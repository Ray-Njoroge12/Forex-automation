from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("fx_ai_engine.metrics")


class Metrics:
    def __init__(self, counters: dict[str, Any], gauges: dict[str, Any]):
        self.counters = counters
        self.gauges = gauges

    def inc(self, name: str, amount: float = 1.0) -> None:
        metric = self.counters.get(name)
        if metric is not None:
            metric.inc(amount)

    def set_gauge(self, name: str, value: float) -> None:
        metric = self.gauges.get(name)
        if metric is not None:
            metric.set(value)


class NoopMetrics(Metrics):
    def __init__(self) -> None:
        super().__init__({}, {})

    def inc(self, name: str, amount: float = 1.0) -> None:
        return None

    def set_gauge(self, name: str, value: float) -> None:
        return None


def init_metrics() -> Metrics:
    if os.getenv("PROM_ENABLED") != "1":
        return NoopMetrics()

    try:
        from prometheus_client import Counter, Gauge, start_http_server
    except Exception as exc:  # pragma: no cover - dependency missing
        logger.warning("Prometheus client not available: %s", exc)
        return NoopMetrics()

    port = int(os.getenv("PROM_PORT", "8000"))
    start_http_server(port)

    counters = {
        "decision_cycles": Counter("fx_decision_cycles_total", "Decision cycles executed"),
        "trades_routed": Counter("fx_trades_routed_total", "Trades routed to MT5"),
        "trades_rejected": Counter("fx_trades_rejected_total", "Trades rejected by gates"),
        "risk_blocks": Counter("fx_risk_blocks_total", "Trades blocked by hard risk engine"),
        "state_stale": Counter("fx_state_stale_total", "State stale halt events"),
    }
    gauges = {
        "open_positions": Gauge("fx_open_positions", "Open positions count"),
        "open_risk_percent": Gauge("fx_open_risk_percent", "Open risk percent"),
    }

    return Metrics(counters, gauges)
