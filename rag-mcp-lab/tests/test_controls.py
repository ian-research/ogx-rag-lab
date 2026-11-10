import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import orchestrator as o
import settings
import transports
from tool_contracts import MODEL_TOOLS, RunFailure, validate_call


def call(name="get_ticket", args='{"ticket_id":"INC-001"}', cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def message(content="完成", calls=None, finish=None):
    return {"choices": [{"finish_reason": finish or ("tool_calls" if calls else "stop"),
                         "message": {"content": content, "tool_calls": calls or []}}]}


@pytest.fixture
def harness(private_config, monkeypatch):
    seen, model_inputs = [], []
    async def discovery(*args): return MODEL_TOOLS
    async def tool(*args):
        seen.append(args)
        return {"status": "ok", "ticket_id": "INC-001"}
    monkeypatch.setattr(transports, "discover", discovery)
    monkeypatch.setattr(transports, "call_tool", tool)
    def setup(responses):
        it = iter(responses)
        async def model(messages, tools):
            model_inputs.append(json.loads(json.dumps(messages)))
            return next(it)
        monkeypatch.setattr(transports, "ask_model", model)
    client = TestClient(o.app)
    def ask(extra=None, token=None):
        return client.post("/api/ask", headers={"Authorization": "Bearer " + (token or private_config["a"])},
                           json={"question": "查询", "as_of": "2026-11-09", **(extra or {})})
    return client, setup, ask, seen, model_inputs


def test_no_enumeration_and_authenticated_session(harness, private_config):
    client, _, _, _, _ = harness
    assert client.get("/api/sessions").status_code == 404
    assert client.get("/api/session").status_code == 401
    response = client.get("/api/session", headers={"Authorization": "Bearer " + private_config["a"]})
    assert response.json() == {"session_id": "a", "label": "a"}
    assert all(t not in response.text for t in private_config.values())


@pytest.mark.parametrize("token", ["", "forged-" + "X" * 40])
def test_auth_rejects_before_model(harness, token):
    client, _, _, seen, inputs = harness
    response = client.post("/api/ask", json={"question": "查询"}, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert not seen and not inputs


@pytest.mark.parametrize("name,args", [
    ("close_ticket", '{}'), ("get_ticket", '{"ticket_id":'), ("get_ticket", '[]'),
    ("get_ticket", '{}'), ("get_ticket", '{"ticket_id":7}'),
    ("get_ticket", '{"ticket_id":null}'), ("get_ticket", '{"ticket_id":""}'),
    ("get_ticket", '{"ticket_id":"INC-001","identity":"other"}'),
    ("get_ticket", '{"ticket_id":"A","ticket_id":"B"}'),
    ("get_ticket", '{"ticket_id":NaN}'),
    ("get_employee_laptop_info", '{"dummy_parameter":"other"}'),
    ("search_documents", '{"question":"q","policy_id":"eval3-device","as_of":"2000-01-01"}'),
    ("search_documents", '{"question":"q","policy_id":"other"}'),
])
def test_invalid_calls_do_not_execute(harness, name, args):
    _, setup, ask, seen, _ = harness
    setup([message(calls=[call(name, args)])])
    response = ask()
    assert response.status_code == 422
    assert response.json()["status"] == "invalid_tool_call"
    assert not seen


def test_batch_is_validated_before_any_execution(harness):
    _, setup, ask, seen, _ = harness
    setup([message(calls=[call(), call("write", '{}', 'c2')])])
    assert ask().status_code == 422
    assert not seen


def test_duplicate_call_ids_rejected(harness):
    _, setup, ask, seen, _ = harness
    setup([message(calls=[call(), call()])])
    assert ask().status_code == 422 and not seen


def test_total_budget_across_rounds(harness, monkeypatch):
    _, setup, ask, seen, _ = harness
    monkeypatch.setattr(settings, "MAX_TOOL_CALLS", 3)
    setup([message(calls=[call(cid="a"), call(cid="b")]), message(calls=[call(cid="c"), call(cid="d")])])
    response = ask()
    assert response.json()["status"] == "budget_exhausted"
    assert len(seen) == 2  # second batch rejected as a unit


def test_step_limit_is_not_success(harness, monkeypatch):
    _, setup, ask, seen, _ = harness
    monkeypatch.setattr(settings, "MAX_STEPS", 2)
    setup([message(calls=[call(cid="a")]), message(calls=[call(cid="b")])])
    assert ask().json()["status"] == "budget_exhausted"
    assert len(seen) == 2


@pytest.mark.parametrize("content,finish,expected", [("", "length", "truncated"),
    ("未完成", "length", "truncated"), ("", "stop", "model_error"), ("x", "content_filter", "model_error")])
def test_incomplete_generation_not_success(harness, content, finish, expected):
    _, setup, ask, _, _ = harness
    setup([message(content=content, finish=finish)])
    response = ask()
    assert response.status_code == 502 and response.json()["status"] == expected


def test_mcp_is_error_preserved():
    with pytest.raises(RunFailure) as error:
        transports.decode_result(SimpleNamespace(isError=True, content=[]))
    assert error.value.status == "tool_error"


@pytest.mark.parametrize("payload", ["not-json", '{"status":"error"}', '{"status":"unauthenticated"}', '[]'])
def test_bad_result_not_business_fact(payload):
    with pytest.raises(RunFailure):
        transports.decode_result(SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=payload)]))


def test_tool_failure_stops_generation(harness, monkeypatch):
    _, setup, ask, _, inputs = harness
    async def fail(*args): raise RunFailure("tool_error", "failed")
    monkeypatch.setattr(transports, "call_tool", fail)
    setup([message(calls=[call()]), message("不应生成")])
    assert ask().json()["status"] == "tool_error"
    assert len(inputs) == 1


def test_total_deadline(harness, monkeypatch):
    _, _, ask, _, _ = harness
    monkeypatch.setattr(settings, "REQUEST_TIMEOUT", .03)
    async def slow(*args): await asyncio.sleep(.2)
    monkeypatch.setattr(transports, "ask_model", slow)
    assert ask().status_code == 504


def test_per_tool_timeout(harness, monkeypatch):
    _, setup, ask, _, _ = harness
    monkeypatch.setattr(settings, "TOOL_TIMEOUT", .02)
    async def slow(*args): await asyncio.sleep(.2)
    monkeypatch.setattr(transports, "call_tool", slow)
    setup([message(calls=[call()])])
    response = ask()
    assert response.status_code == 504 and response.json()["status"] == "timeout"


def test_date_and_identity_are_application_owned(harness, private_config):
    _, setup, ask, seen, _ = harness
    setup([message(calls=[call("search_documents", '{"question":"q","policy_id":"eval3-device"}')]), message()])
    assert ask(token=private_config["b"]).status_code == 200
    assert seen[0][2:] == ("john.doe@company.com", "2026-11-09")


def test_credential_redaction_and_private_evidence(harness, private_config):
    _, setup, ask, _, inputs = harness
    token = private_config["a"]
    setup([message("结果 " + token)])
    response = ask({"question": "误粘贴 " + token})
    assert token not in response.text and token not in json.dumps(inputs)
    files = list(settings.RUN_ROOT.glob('*.json'))
    assert len(files) == 1 and files[0].stat().st_mode & 0o077 == 0
    assert token not in files[0].read_text()
    assert 'identity_token' not in files[0].read_text()


@pytest.mark.parametrize("extra", [{"session": "private-value"}, {"as_of": "2026-99-01"},
    {"as_of": "20261109"}, {"question": ["q"]}, {"question": " "}])
def test_request_validation_does_not_echo_inputs(harness, extra):
    _, _, ask, _, _ = harness
    response = ask(extra)
    assert response.status_code == 422 and "private-value" not in response.text


def test_oversized_cumulative_evidence(harness, monkeypatch):
    _, setup, ask, _, _ = harness
    monkeypatch.setattr(settings, "MAX_TOTAL_TOOL_BYTES", 5)
    setup([message(calls=[call()])])
    assert ask().json()["status"] == "result_too_large"


def test_evidence_write_failure_not_success(harness, monkeypatch):
    _, setup, ask, _, _ = harness
    def fail(*args): raise OSError()
    monkeypatch.setattr(o, "write_private", fail)
    setup([message()])
    assert ask().json()["status"] == "evidence_error"


def test_browser_has_no_credential_discovery_or_storage():
    html = (settings.ROOT / 'web/index.html').read_text()
    assert '/api/sessions' not in html
    assert 'localStorage' not in html and 'sessionStorage' not in html
    assert 'type="password"' in html and "Bearer " in html
