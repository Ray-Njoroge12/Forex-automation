from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("fx_ai_engine.observability")

_initialized = False


def init_tracing(service_name: str = "fx_ai_engine") -> Any:
    """Initialize OpenTelemetry tracing when enabled. Returns a tracer-like object."""
    global _initialized

    if os.getenv("OTEL_ENABLED") != "1":
        return _noop_tracer()

    if _initialized:
        return _get_tracer(service_name)

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
    except Exception as exc:  # pragma: no cover - dependency missing
        logger.warning("OpenTelemetry not available: %s", exc)
        return _noop_tracer()

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    processor = SimpleSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    _initialized = True

    return trace.get_tracer(service_name)


def _get_tracer(service_name: str) -> Any:
    try:
        from opentelemetry import trace

        return trace.get_tracer(service_name)
    except Exception:  # pragma: no cover - dependency missing
        return _noop_tracer()


class _noop_span:
    def __enter__(self) -> "_noop_span":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def set_attribute(self, *_: Any, **__: Any) -> None:
        return None


class _noop_tracer:
    def start_as_current_span(self, *_: Any, **__: Any) -> _noop_span:
        return _noop_span()
