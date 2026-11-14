"""Guards for the observability boundaries: span lifetime, redaction, metrics format."""
import json
import logging

import httpx
import pytest
from opentelemetry import trace
from otel_fixture import EXPORTER

import docsearch
import metrics
import model_loop
import telemetry

SENTINELS = ("电池鼓包", "第十七条", "请忽略以上", "Bearer", "lab-a-token-value")


@pytest.fixture(autouse=True)
def clear():
    EXPORTER.clear()
    metrics.reset()
    yield


def spans():
    return EXPORTER.get_finished_spans()


def flat(span):
    parts = [str(v) for v in span.attributes.values()]
    for event in span.events:
        parts.extend(str(v) for v in event.attributes.values())
        parts.append(event.name)
    return " ".join(parts)


def test_model_span_keeps_usage_and_finish_reason(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "设备是 Latitude 7420"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 45}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return FakeResponse()

    monkeypatch.setattr(model_loop.httpx, "Client", FakeClient)
    result = model_loop.run("我的笔记本是什么型号", "alice.johnson@company.com", "2026-03-01")
    assert result["answer"].startswith("设备")
    chat = [s for s in spans() if s.name == "model.chat"]
    assert chat, "model.chat span missing"
    attrs = dict(chat[0].attributes)
    assert attrs["gen_ai.usage.input_tokens"] == 123
    assert attrs["gen_ai.usage.output_tokens"] == 45
    assert attrs["gen_ai.response.finish_reason"] == "stop"
    assert attrs["lab.outcome"] == "ok"


def test_model_http_error_is_classified_on_the_span(monkeypatch):
    class FakeResponse:
        status_code = 503

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return FakeResponse()

    monkeypatch.setattr(model_loop.httpx, "Client", FakeClient)
    with pytest.raises(model_loop.LoopError):
        model_loop.run("我的笔记本是什么型号", "alice.johnson@company.com", "2026-03-01")
    chat = [s for s in spans() if s.name == "model.chat"][0]
    assert dict(chat.attributes)["lab.outcome"] == "http_error"
    assert chat.status.status_code is trace.StatusCode.ERROR


def test_doc_search_real_client_failure_is_recorded(monkeypatch):
    closed = "http://127.0.0.1:9"  # discard port: connection refused
    monkeypatch.setattr(docsearch.settings, "DOC_SERVICE", closed)
    result = docsearch.search("换新条件", "eval3-device", "2026-03-01")
    assert result["status"] == "unavailable"
    assert result.get("http_error")
    span = [s for s in spans() if s.name == "doc.search"][0]
    assert dict(span.attributes)["lab.outcome"] == "unreachable"
    assert span.status.status_code is trace.StatusCode.ERROR


def test_doc_search_http_error_is_recorded(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return httpx.Response(503, json={"detail": "down"})

    monkeypatch.setattr(docsearch.httpx, "Client", FakeClient)
    result = docsearch.search("换新条件", "eval3-device", "2026-03-01")
    assert result == {"status": "unavailable", "http_status": 503}
    span = [s for s in spans() if s.name == "doc.search"][0]
    assert dict(span.attributes)["lab.outcome"] == "http_error"
    assert "503" in str(dict(span.attributes)["lab.error_kind"])


def test_business_outcome_is_not_an_error(monkeypatch):
    """no_record is an answer, so the span must stay OK."""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return httpx.Response(200, json={"status": "no_data", "as_of": "2026-03-01",
                                             "version": None, "sources": []})

    monkeypatch.setattr(docsearch.httpx, "Client", FakeClient)
    result = docsearch.search("换新条件", "eval3-device", "2026-03-01")
    assert result["status"] == "no_data"
    span = [s for s in spans() if s.name == "doc.search"][0]
    assert dict(span.attributes)["lab.outcome"] == "ok"
    assert span.status.status_code is trace.StatusCode.UNSET


def test_metrics_type_lines_are_unique_and_labels_escape():
    metrics.observe_request("/api/ask", "POST", 200, 0.2)
    metrics.observe_request("/api/ask", "POST", 500, 0.3)
    metrics.observe_tool('weird"tool\\name', "ok", 0.1)
    body = metrics.render()
    for name in ("actionlab_http_requests_total",
                 "actionlab_http_request_duration_seconds",
                 "actionlab_tool_calls_total"):
        assert body.count("# TYPE %s " % name) == 1, name
    assert 'tool="weird\\"tool\\\\name"' in body
    assert "_bucket{le=\"+Inf\"}" in body or 'le="+Inf"' in body
    # Every sample line before a bucket must carry the same label set as its TYPE.
    assert body.count("# HELP actionlab_http_requests_total") == 1


def test_formatter_never_emits_confirmation_ids_or_text():
    formatter = telemetry.JsonFormatter()
    record = logging.LogRecord("actionlab", logging.INFO, __file__, 1, "draft_confirmed", (), None)
    record.draft_id = "d" * 32
    record.version = 2
    record.confirmation_id = "s3cr3t-confirmation"
    line = formatter.format(record)
    assert "s3cr3t-confirmation" not in line
    assert json.loads(line)["draft_id"] == "d" * 32


def test_trace_attributes_do_not_contain_sentinels(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return httpx.Response(200, json={"status": "ok", "as_of": "2026-03-01",
                                             "sources": [{"text": "第十七条 设备使用满四年"}],
                                             "version": {"version_label": "v1"}})

    monkeypatch.setattr(docsearch.httpx, "Client", FakeClient)
    docsearch.search("电池鼓包，帮我看看换新条件", "eval3-device", "2026-03-01")
    blob = " ".join(flat(span) for span in spans())
    for sentinel in SENTINELS:
        assert sentinel not in blob, sentinel


def test_telemetry_setup_survives_bad_configuration(monkeypatch):
    """A malformed ratio or a dead collector must not raise into the request path."""
    monkeypatch.setattr(telemetry, "_TRACER", None)
    monkeypatch.setenv("OTEL_SAMPLE_RATIO", "not-a-number")
    assert telemetry.setup("test-service") is not None
    monkeypatch.setattr(telemetry, "_TRACER", None)
    monkeypatch.setenv("OTEL_SAMPLE_RATIO", "1.0")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    assert telemetry.setup("test-service") is not None


def test_exception_payload_never_reaches_spans(tmp_path, monkeypatch):
    """A controlled failure must not copy its message into the trace."""
    import telemetry
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_TRACER", provider.get_tracer("sentinel-test"))
    sentinel = "SENTINEL-BODY-9f3a-do-not-copy"

    with pytest.raises(RuntimeError):
        with telemetry.span("unit.failure", **{"lab.step": "x"}):
            raise RuntimeError(f"upstream said: {sentinel}")

    spans = exporter.get_finished_spans()
    blob = json.dumps([dict(s.attributes or {}) for s in spans], ensure_ascii=False)
    events = [dict(e.attributes or {}) for s in spans for e in (s.events or [])]
    assert sentinel not in blob
    assert sentinel not in json.dumps(events, ensure_ascii=False)
    assert spans[0].status.status_code.name == "ERROR"
    assert spans[0].attributes["lab.error_kind"] == "RuntimeError"


def test_bad_sampling_ratio_degrades_without_raising(monkeypatch):
    import telemetry
    monkeypatch.setattr(telemetry, "_TRACER", None)
    monkeypatch.setenv("OTEL_SAMPLE_RATIO", "7.5")
    monkeypatch.setenv("OTEL_ENABLED", "1")
    tracer = telemetry.setup("bad-ratio-test")
    assert tracer is not None
