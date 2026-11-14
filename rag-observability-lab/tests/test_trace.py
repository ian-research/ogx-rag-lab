"""Tracing behaviour: names, parent links, stored ids, and what must not leak."""
import json

import pytest
from opentelemetry import trace
from otel_fixture import EXPORTER

from conftest import auth


@pytest.fixture(autouse=True)
def stub_model(monkeypatch):
    import model_loop

    def fake_run(question, identity, as_of=None):
        import devices
        device = devices.lookup(identity)
        docs = {"status": "ok",
                "version": {"policy_id": "eval3-device", "filename": "eval3-device.md",
                            "version_label": "v1", "valid_from": "2026-01-01"},
                "sources": [{"valid_to": None, "heading": "换新条件",
                             "text": "第十七条 设备使用满四年且维修成本超过重置价一半的，可申请更换。"}]}
        return {"answer": "查到设备台账与换新条件；缺少领用日期与维修成本。", "steps": [],
                "device": device, "docs": docs,
                "as_of": as_of or "2026-03-01",
                "user_reason": "电池鼓包"}

    monkeypatch.setattr(model_loop, "run", fake_run)


@pytest.fixture()
def spans():
    EXPORTER.clear()
    return EXPORTER


def _prepare(client, tokens):
    response = client.post("/api/ask",
                           json={"question": "我的电池鼓包，想申请更换笔记本，帮我准备",
                                 "as_of": "2026-03-01"},
                           headers=auth(tokens))
    assert response.status_code == 200, response.text
    return response.json()


def test_ask_span_and_stored_trace_id(client, tokens, spans):
    body = _prepare(client, tokens)
    draft = body["draft"]
    names = [span.name for span in spans.get_finished_spans()]
    assert "ask" in names and "draft.build" in names
    ask_span = next(span for span in spans.get_finished_spans() if span.name == "ask")
    trace_id = format(ask_span.get_span_context().trace_id)
    assert body["trace_id"] == trace_id

    links = client.get(f"/api/trace/{draft['draft_id']}", headers=auth(tokens)).json()["links"]
    assert links["draft"]["trace_id"] == trace_id
    assert links["draft"]["status"] == "awaiting_confirmation"


def test_confirm_and_submit_are_separate_traces(client, tokens, spans):
    body = _prepare(client, tokens)
    draft = body["draft"]
    ask_trace = body["trace_id"]
    confirmed = client.post(f"/api/drafts/{draft['draft_id']}/confirm",
                            json={"version": draft["version"],
                                  "content_hash": draft["content_hash"]},
                            headers=auth(tokens)).json()
    assert confirmed["trace_id"] and confirmed["trace_id"] != ask_trace
    submitted = client.post(f"/api/drafts/{draft['draft_id']}/submit",
                            json={"confirmation_id": confirmed["confirmation_id"]},
                            headers=auth(tokens))
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted"

    links = client.get(f"/api/trace/{draft['draft_id']}", headers=auth(tokens)).json()["links"]
    assert links["confirmation"]["trace_id"] == confirmed["trace_id"]
    assert links["submission"]["trace_id"]
    assert links["submission"]["trace_id"] not in (ask_trace, confirmed["trace_id"])
    assert links["submission"]["state"] == "submitted"


def test_spans_do_not_carry_business_text_or_keys(client, tokens, spans):
    body = _prepare(client, tokens)
    draft = body["draft"]
    confirmed = client.post(f"/api/drafts/{draft['draft_id']}/confirm",
                            json={"version": draft["version"],
                                  "content_hash": draft["content_hash"]},
                            headers=auth(tokens)).json()
    client.post(f"/api/drafts/{draft['draft_id']}/submit",
                json={"confirmation_id": confirmed["confirmation_id"]}, headers=auth(tokens))

    blob = json.dumps([{key: str(value) for key, value in span.attributes.items()}
                       for span in spans.get_finished_spans()], ensure_ascii=False)
    for secret in ("电池鼓包", "第十七条", draft["content_hash"], "Bearer", "k" * 48):
        assert secret not in blob
    assert "lab.question_chars" in blob


def test_metrics_endpoint_reports_requests(client, tokens):
    _prepare(client, tokens)
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert "actionlab_http_requests_total{method=\"POST\",route=\"/api/ask\",status=\"200\"}" in body
    assert "actionlab_http_request_duration_seconds_bucket" in body


def test_trace_endpoint_hides_other_users_draft(client, tokens):
    body = _prepare(client, tokens)
    response = client.get(f"/api/trace/{body['draft']['draft_id']}",
                          headers=auth(tokens, "lab-b"))
    assert response.status_code == 404


def format(trace_id):
    return "%032x" % trace_id
