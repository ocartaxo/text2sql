# src/text2sql/adapters/telemetry.py
from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic_ai import Agent

from text2sql.config import Settings


def _parse_otlp_headers(raw_headers: str | None) -> dict[str, str] | None:
    if not raw_headers or not raw_headers.strip():
        return None
    parsed: dict[str, str] = {}
    for pair in raw_headers.split(","):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed or None


def _build_traces_endpoint(base_endpoint: str) -> str:
    cleaned = base_endpoint.rstrip("/")
    if cleaned.endswith("/v1/traces"):
        return cleaned
    return f"{cleaned}/v1/traces"


def configure_telemetry(
    settings: Settings,
    app: FastAPI | None = None,
) -> TracerProvider | None:
    """Inicializa TracerProvider OTLP/HTTP e instrumenta FastAPI + PydanticAI."""
    if not settings.enable_otel:
        return None

    resource = Resource.create({SERVICE_NAME: settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=_build_traces_endpoint(settings.otel_exporter_otlp_endpoint),
        headers=_parse_otlp_headers(settings.otel_exporter_otlp_headers),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    Agent.instrument_all()

    if app is not None:
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            excluded_urls="/health",
        )

    return provider


def shutdown_telemetry(provider: TracerProvider | None) -> None:
    if provider is not None:
        provider.force_flush()
        provider.shutdown()