"""OpenTelemetry helpers layered onto the existing Strands telemetry surface."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Any, Dict, Iterator, Optional

from opentelemetry import trace as trace_api

_OTEL_INIT_LOCK = threading.Lock()
_OTEL_CONFIGURED = False
_OTEL_CONFIGURATION_KEY: Optional[tuple[Any, ...]] = None


def configure_strands_otel(
    *,
    enabled: bool,
    enable_traces: bool,
    enable_metrics: bool,
    otlp_endpoint: Optional[str],
    service_name: Optional[str],
    headers: Optional[Dict[str, str]],
    logger: logging.Logger,
) -> bool:
    """Initialize Strands/OpenTelemetry exporters once per process when enabled."""
    global _OTEL_CONFIGURED, _OTEL_CONFIGURATION_KEY

    if not enabled:
        return False

    configuration_key = (
        bool(enable_traces),
        bool(enable_metrics),
        str(otlp_endpoint or ""),
        str(service_name or ""),
        tuple(sorted((headers or {}).items())),
    )

    with _OTEL_INIT_LOCK:
        if _OTEL_CONFIGURED and _OTEL_CONFIGURATION_KEY == configuration_key:
            return True

        from strands.telemetry.config import StrandsTelemetry

        if service_name:
            os.environ.setdefault("OTEL_SERVICE_NAME", service_name)

        telemetry = StrandsTelemetry()
        otlp_kwargs: Dict[str, Any] = {}
        if otlp_endpoint:
            otlp_kwargs["endpoint"] = otlp_endpoint
        if headers:
            otlp_kwargs["headers"] = headers

        if enable_traces:
            telemetry.setup_otlp_exporter(**otlp_kwargs)
            logger.info("Enabled Strands OTEL trace export.")
        if enable_metrics:
            telemetry.setup_meter(enable_otlp_exporter=True)
            logger.info("Enabled Strands OTEL metrics export.")

        _OTEL_CONFIGURED = True
        _OTEL_CONFIGURATION_KEY = configuration_key
        return True


def current_trace_context() -> tuple[Optional[str], Optional[str]]:
    """Return current trace/span ids when an OpenTelemetry span is active."""
    span = trace_api.get_current_span()
    context = span.get_span_context()
    if context is None or not context.is_valid:
        return None, None
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


@contextlib.contextmanager
def start_runtime_span(
    name: str,
    *,
    enabled: bool,
    attributes: Optional[Dict[str, Any]] = None,
) -> Iterator[None]:
    """Start a lightweight wrapper span when OTEL is enabled."""
    if not enabled:
        yield
        return
    tracer = trace_api.get_tracer("team_nats_agents.runtime")
    with tracer.start_as_current_span(name, attributes=attributes or {}):
        yield
