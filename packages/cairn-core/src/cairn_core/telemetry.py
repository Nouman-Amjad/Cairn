"""Structured logging and OpenTelemetry wiring.

One instrumentation, two backends: the OTLP stream feeds both the collector
(Grafana/Tempo) and Langfuse. Span attribute names follow the `gen_ai.*`
semantic conventions where they exist so Grafana's LLM panels work unmodified.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, Status, StatusCode

from cairn_core.config import TelemetrySettings
from cairn_core.sensitivity import redact

_configured = False


def setup(cfg: TelemetrySettings) -> None:
    """Idempotent. Services call this once at startup."""
    global _configured
    if _configured:
        return
    _setup_logging(cfg)
    _setup_tracing(cfg)
    _configured = True


def _setup_logging(cfg: TelemetrySettings) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=cfg.log_level.upper())
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_processor,
        _trace_context_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if cfg.json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[cfg.log_level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _redact_processor(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    """Nothing reaches stdout with PII in it. Prompts and completions are not
    logged at all; this catches the accidental cases."""
    for key, value in event.items():
        if isinstance(value, str) and len(value) < 20_000:
            event[key] = redact(value)
    return event


def _trace_context_processor(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event["trace_id"] = format(ctx.trace_id, "032x")
        event["span_id"] = format(ctx.span_id, "016x")
    return event


def _setup_tracing(cfg: TelemetrySettings) -> None:
    if not cfg.endpoint and not cfg.langfuse_host:
        return

    provider = TracerProvider(
        resource=Resource.create({"service.name": cfg.service_name}),
        sampler=ParentBased(TraceIdRatioBased(cfg.sample_ratio)),
    )

    if cfg.endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=cfg.endpoint, insecure=True))
        )

    # Two backends, one instrumentation. Langfuse ingests the same OTLP
    # stream over HTTP for prompt-level inspection and eval scoring; the
    # collector gets it over gRPC for Grafana and Tempo. Emitting twice from
    # the application would mean two sets of attributes to keep in step.
    if langfuse := _langfuse_exporter(cfg):
        provider.add_span_processor(BatchSpanProcessor(langfuse))

    trace.set_tracer_provider(provider)


def _langfuse_exporter(cfg: TelemetrySettings) -> Any | None:
    if not (cfg.langfuse_host and cfg.langfuse_public_key and cfg.langfuse_secret_key):
        return None
    try:
        import base64

        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HTTPSpanExporter,
        )
    except ImportError:
        log = get_logger(__name__)
        log.warning("langfuse_exporter_unavailable", reason="otlp http exporter not installed")
        return None

    credentials = base64.b64encode(
        f"{cfg.langfuse_public_key.get_secret_value()}:"
        f"{cfg.langfuse_secret_key.get_secret_value()}".encode()
    ).decode()
    return HTTPSpanExporter(
        endpoint=f"{cfg.langfuse_host.rstrip('/')}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {credentials}"},
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def tracer(name: str = "cairn") -> trace.Tracer:
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Span]:
    with tracer().start_as_current_span(name) as sp:
        for key, value in attrs.items():
            if value is not None:
                sp.set_attribute(key, value)
        try:
            yield sp
        except Exception as exc:
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            sp.record_exception(exc)
            raise


def record_llm_call(
    sp: Span,
    *,
    model: str,
    route: str,
    route_reason: str,
    tokens_in: int,
    tokens_out: int,
    cached_read_tokens: int = 0,
    cost_usd: Decimal | float = 0,
    task_class: str | None = None,
) -> None:
    """Attach the gen_ai + cairn attributes to an llm.call span.

    Prompt and completion text are deliberately absent. Langfuse gets those
    over its own channel with its own redaction; a trace attribute is the
    wrong place for restricted data.
    """
    sp.set_attributes(
        {
            "gen_ai.system": "anthropic" if route.startswith("cloud") else "vllm",
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": tokens_in,
            "gen_ai.usage.output_tokens": tokens_out,
            "gen_ai.usage.cached_input_tokens": cached_read_tokens,
            "cairn.route": route,
            "cairn.route_reason": route_reason,
            "cairn.cost_usd": float(cost_usd),
        }
    )
    if task_class:
        sp.set_attribute("cairn.task_class", task_class)


def bind(**kv: Any) -> None:
    """Bind values onto every subsequent log line in this task."""
    structlog.contextvars.bind_contextvars(**kv)


def unbind(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


def carrier_from_context() -> dict[str, str]:
    """W3C traceparent headers for propagation across HTTP hops."""
    from opentelemetry.propagate import inject

    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


def context_from_carrier(headers: Mapping[str, str]) -> Any:
    from opentelemetry.propagate import extract

    return extract(dict(headers))
