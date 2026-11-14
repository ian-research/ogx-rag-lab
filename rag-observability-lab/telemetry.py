"""Tracing and structured logs for the article-6 observability lab.

Deliberate boundaries: spans carry identifiers, counts, durations and statuses.
They never carry the user's question, the draft payload, ticket text or any key.
That keeps a trace useful for "where did it slow down / fail" without turning
the trace backend into a second copy of the business data.
"""
import asyncio
import functools
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import format_span_id, format_trace_id, get_current_span

_TRACER = None
_LOG_READY = False
MAX_ATTR = 200
SERVICE_VERSION = os.getenv("OTEL_SERVICE_VERSION", "article06-r2")


def _enabled():
    return os.getenv("OTEL_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def setup(service):
    """Create the tracer once per process. Safe to call from both services."""
    global _TRACER
    if _TRACER is not None:
        return _TRACER
    name = os.getenv("OTEL_SERVICE_NAME", service)
    if _enabled():
        # Configuration errors must not reach the caller: a broken collector or a
        # malformed ratio degrades to no tracing, it never fails a business call.
        try:
            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318").rstrip("/")
            ratio = float(os.getenv("OTEL_SAMPLE_RATIO", "1.0"))
            if not 0.0 <= ratio <= 1.0:
                raise ValueError("OTEL_SAMPLE_RATIO must be between 0 and 1")
            resource = Resource.create({
                "service.name": name,
                "service.version": SERVICE_VERSION,
                "service.namespace": "article06-observability-lab",
                "deployment.environment": os.getenv("OTEL_ENV", "single-host-lab"),
            })
            provider = TracerProvider(resource=resource,
                                      sampler=ParentBased(TraceIdRatioBased(ratio)))
            provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint + "/v1/traces", timeout=5),
                schedule_delay_millis=1000, max_export_batch_size=128))
            trace.set_tracer_provider(provider)
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the request
            print(json.dumps({"event": "telemetry_setup_failed",
                              "error": type(exc).__name__}), flush=True)
    _TRACER = trace.get_tracer(name)
    setup_logging()
    return _TRACER


def tracer():
    return _TRACER or setup("action-lab")


def _clean(value):
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    text = str(value)
    return text[:MAX_ATTR]


@contextmanager
def span(name, **attributes):
    # Exception payloads can echo request bodies, so they are never recorded:
    # the span keeps a controlled error kind and the traceback stays in the app log.
    with tracer().start_as_current_span(name, record_exception=False,
                                        set_status_on_exception=False) as current:
        for key, value in attributes.items():
            cleaned = _clean(value)
            if cleaned is not None:
                current.set_attribute(key, cleaned)
        try:
            yield current
        except Exception as exc:  # noqa: BLE001 - re-raised after classification
            mark_error(current, type(exc).__name__)
            raise


def set_attrs(current, **attributes):
    if current is None:
        return
    for key, value in attributes.items():
        cleaned = _clean(value)
        if cleaned is not None:
            current.set_attribute(key, cleaned)


def mark_error(current, kind, description=None):
    """Record a controlled error class. Never pass an exception message through:
    it can contain request bodies or upstream payloads."""
    if current is None:
        return
    try:
        current.set_attribute("lab.error_kind", kind)
        if description:
            current.set_attribute("lab.error_description", description[:MAX_ATTR])
        current.set_status(trace.Status(trace.StatusCode.ERROR, kind))
    except Exception:  # noqa: BLE001
        pass


def current_ids():
    context = get_current_span().get_span_context()
    if not context.is_valid:
        return {"trace_id": None, "span_id": None}
    return {"trace_id": format_trace_id(context.trace_id),
            "span_id": format_span_id(context.span_id)}


class JsonFormatter(logging.Formatter):
    """One JSON object per line so journald output can be joined on trace_id."""

    # A confirmation id is a capability: knowing it lets the holder submit the
    # draft. Logs carry draft_id + version instead, which is enough to join.
    FIELDS = ("route", "method", "status", "duration_ms", "run_id", "draft_id",
              "version", "idem", "ticket_no", "tool", "model", "error",
              "http_status", "event_detail")

    def format(self, record):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key in self.FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        ids = current_ids()
        if ids["trace_id"]:
            payload["trace_id"] = ids["trace_id"]
            payload["span_id"] = ids["span_id"]
        return json.dumps(payload, ensure_ascii=False)


def setup_logging():
    global _LOG_READY
    if _LOG_READY:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel("WARNING")
    _LOG_READY = True


def log_event(event, level=logging.INFO, **fields):
    setup_logging()
    logging.getLogger("actionlab").log(level, event, extra=fields)


def traced(name, **attributes):
    """Span decorator that keeps the original signature visible to FastMCP."""
    def decorate(function):
        if asyncio.iscoroutinefunction(function):
            @functools.wraps(function)
            async def async_inner(*args, **kwargs):
                with span(name, **attributes):
                    return await function(*args, **kwargs)
            return async_inner

        @functools.wraps(function)
        def inner(*args, **kwargs):
            with span(name, **attributes):
                return function(*args, **kwargs)
        return inner
    return decorate
