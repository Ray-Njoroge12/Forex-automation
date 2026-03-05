from __future__ import annotations

from core.observability import init_tracing


def test_tracing_noop_when_disabled():
    tracer = init_tracing("fx_ai_engine_test")
    with tracer.start_as_current_span("noop-span"):
        assert True
