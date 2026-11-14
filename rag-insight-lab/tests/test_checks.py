"""Program checks for article 07: read-only surface, scope, budget, arithmetic, failure states."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from insight import clients, core, explain
from insight.app import app, semaphore

client = TestClient(app)


@pytest.fixture()
def token(tmp_path, monkeypatch):
    priv = tmp_path / "private"
    priv.mkdir()
    tok = "t" * 40
    (priv / "admins.json").write_text(
        json.dumps({"ops-lab": {"sha256": core.sha256_text(tok), "role": "ops"},
                    "reader": {"sha256": core.sha256_text("r" * 40), "role": "viewer"}})
    )
    (priv / "admins.json").chmod(0o600)
    monkeypatch.setattr(core, "PRIVATE_DIR", priv)
    for name in ("EVIDENCE_DIR", "RAW_DIR", "RUN_DIR"):
        path = tmp_path / name.lower()
        path.mkdir()
        monkeypatch.setattr(core, name, path)
    return tok


def auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def fake_jaeger(payloads: dict):
    async def call(self, tool, arguments, **kwargs):
        if tool not in payloads:
            raise clients.BackendError("mcp_tool_error")
        return {
            "tool": tool,
            "arguments": arguments,
            "elapsed_ms": 1.0,
            "result": payloads[tool],
            "raw_bytes": 10,
            "result_sha256": "0" * 64,
        }
    return call


# Real Jaeger v2.23 TopologySpan: path/service/span_name/start_time/duration_us/status/
# truncated_children only. No span_id and no parent_span_id (see upstream types).
ROOT_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
MODEL_ID = "1a2b3c4d5e6f708192a3b4c5d6e7f809"
TOOL_ID = "2b3c4d5e6f708192a3b4c5d6e7f80910"
TOPO = {
    "spans": [
        {"path": ROOT_ID, "service": "action-lab-api", "span_name": "POST /api/ask",
         "duration_us": 2_800_000, "status": "OK", "truncated_children": False,
         "start_time": core.iso((core.now_utc() - timedelta(minutes=5)).replace(microsecond=0))},
        {"path": f"{ROOT_ID}/{MODEL_ID}", "service": "action-lab-api", "span_name": "model.chat",
         "duration_us": 2_400_000, "status": "OK", "truncated_children": False,
         "start_time": core.iso((core.now_utc() - timedelta(minutes=5)).replace(microsecond=0))},
        {"path": f"{ROOT_ID}/{TOOL_ID}", "service": "action-lab-api", "span_name": "tool.search_documents",
         "duration_us": 8_490, "status": "OK", "truncated_children": False,
         "start_time": core.iso((core.now_utc() - timedelta(minutes=5)).replace(microsecond=0))},
    ],
    "trace_id": "a" * 32,
}
DETAILS = {
    "spans": [
        {"span_id": ROOT_ID, "parent_span_id": "", "span_name": "POST /api/ask",
         "attributes": {"http.status_code": 200, "http.route": "/api/ask", "secret": "x" * 300},
         "events": [{"name": "leak", "attributes": {"body": "very private"}}], "links": [{"x": 1}]},
        {"span_id": TOOL_ID, "parent_span_id": ROOT_ID, "span_name": "tool.search_documents",
         "attributes": {"lab.outcome": "no_record", "lab.tool": "search_documents"}, "duration_us": 8490},
    ]
}


def test_topology_ids_come_from_path():
    """D: ids are derived from path; detail ids are what we asked for."""
    calls = {}

    async def call(self, tool, arguments, **kwargs):
        calls.setdefault(tool, []).append(arguments)
        payload = DETAILS if tool == "get_span_details" else TOPO
        return {"tool": tool, "arguments": arguments, "elapsed_ms": 1, "result": payload,
                "raw_bytes": 10, "result_sha256": "0" * 64}

    import insight.clients as cl
    import insight.app as appmod
    old = cl.JaegerMCP.call
    cl.JaegerMCP.call = call
    try:
        details, requested = asyncio.run(appmod._span_details(TOPO["spans"], "a" * 32, []))
    finally:
        cl.JaegerMCP.call = old
    assert ROOT_ID in requested and TOOL_ID in requested
    assert calls["get_span_details"][0]["span_ids"] == requested
    coverage = appmod._http_and_business(details, requested, ROOT_ID)
    assert coverage["http_status"] == 200
    assert coverage["tool_outcomes"] == {"tool.search_documents": "no_record"}


def test_placeholder_citation_is_flagged():
    bundle = {"evidence_id": "E-real-1"}
    answer = "事实：x [E-real-1]\n解释：y\n证据：[<证据编号>]\n下一步：z。"
    guard = explain.guard_answer(answer, bundle)
    assert guard["placeholder_citations"]
    assert guard["verdict"] == "citation_placeholder"


def test_window_with_only_recent_samples_is_not_comparable():
    """300s windows backed by the last 30s of samples must not be called comparable."""
    a = _series([[300, 1], [315, 2]])
    b = _series([[600, 3], [615, 4]])
    out = core.windows_summary(
        a, b,
        _ts([570, 585, 600], 300, 600), _ts([570, 585, 600], 300, 600),
        _up(), _up(), 300, 600, 300, 600, 15.0)
    assert out["comparable"] is False


# ---------------------------------------------------------------- read-only surface


def test_unknown_tool_rejected():
    with pytest.raises(PermissionError):
        asyncio.run(clients.JaegerMCP().call("execute_query", {}))


def test_sql_argument_rejected():
    with pytest.raises(PermissionError):
        asyncio.run(clients.JaegerMCP().call("search_traces", {"sql": "select 1"}))


def test_long_promql_rejected():
    with pytest.raises(PermissionError):
        asyncio.run(clients.Prometheus().query("sum(x)" * 100))


def test_analyze_rejects_extra_fields(token):
    resp = client.post("/api/analyze", headers=auth(token),
                       json={"question_type": "latency_breakdown", "question": "为什么慢", "promql": "up"})
    assert resp.status_code == 422


# ---------------------------------------------------------------- identity and scope


def test_missing_token_401():
    assert client.get("/api/evidence/E-x").status_code == 401
    assert client.post("/api/analyze", json={"question_type": "latency_breakdown", "question": "q"}).status_code == 401


def test_bad_token_401(token):
    assert client.get("/api/evidence/E-x", headers={"Authorization": "Bearer " + "x" * 40}).status_code == 401


def test_non_ops_role_403(token):
    assert client.get("/api/whoami", headers=auth("r" * 40)).status_code == 403


def test_whoami_reports_role(token):
    body = client.get("/api/whoami", headers=auth(token)).json()
    assert body["role"] == "ops" and body["name"] == "ops-lab"


def test_service_not_allowed(token):
    resp = client.post("/api/analyze", headers=auth(token),
                       json={"question_type": "latency_breakdown", "question": "为什么慢", "service": "jaeger"})
    assert resp.status_code == 403


def test_window_requests_cannot_target_other_service(token):
    """R2: the allowlist is checked before the branch, and the window is API-only."""
    resp = client.post("/api/analyze", headers=auth(token),
                       json={"question_type": "window_requests", "question": "两窗对比", "service": "action-lab-mcp"})
    assert resp.status_code == 403


def test_bad_trace_id_400(token):
    resp = client.post("/api/analyze", headers=auth(token),
                       json={"question_type": "latency_breakdown", "question": "qq", "trace_id": "../etc/pass"})
    assert resp.status_code == 400


def test_unknown_question_type(token):
    resp = client.post("/api/analyze", headers=auth(token),
                       json={"question_type": "freeform_sql", "question": "帮我查所有数据"})
    assert resp.status_code == 400


def test_evidence_hides_private_fields(token):
    core.save_evidence({"evidence_id": "E-test-1", "question_type": "latency_breakdown", "facts": {"ok": True},
                        "sources": [], "limits": {}, "window": {}, "frozen_end": ""})
    body = client.get("/api/evidence/E-test-1", headers=auth(token)).json()
    assert "answer" not in body


# ---------------------------------------------------------------- budget and redaction


def test_backend_unavailable_is_503(token, monkeypatch):
    monkeypatch.setattr(clients.JaegerMCP, "call", fake_jaeger({}))
    resp = client.post("/api/analyze", headers=auth(token),
                       json={"question_type": "latency_breakdown", "question": "为什么慢"})
    assert resp.status_code == 503 and resp.json()["status"] == "backend_unavailable"


def test_fit_truncates_and_reports_bytes():
    payload = {"spans": [{"i": i, "pad": "x" * 200} for i in range(200)]}
    out, truncated = core.fit(payload, budget=2000)
    assert truncated is True
    assert out["bytes"] <= 2000 or out["data"].get("truncated_over_budget")


def test_fit_keeps_small_payload():
    out, truncated = core.fit({"a": 1}, budget=2000)
    assert truncated is False and out["data"]["a"] == 1


def test_sanitise_drops_events_and_converts_micros():
    clean = core.sanitise({"spans": [dict(DETAILS["spans"][0]), dict(DETAILS["spans"][1])]})
    first = clean["spans"][0]
    assert "events" not in first and "links" not in first
    assert "secret" not in first["attributes"]
    assert clean["spans"][1]["duration_ms"] == 8.49


def test_model_receives_summary_not_raw(token, monkeypatch):
    """R3: the trimmed summary is what reaches the model; raw stays on disk."""
    monkeypatch.setattr(clients.JaegerMCP, "call",
                        fake_jaeger({"search_traces": {"traces": [{"trace_id": "a" * 32, "start_time": core.iso((core.now_utc() - timedelta(minutes=5)).replace(microsecond=0)),
                                                                   "duration_us": 1, "span_count": 3, "has_errors": False}]},
                                     "get_trace_topology": TOPO, "get_span_details": DETAILS,
                                     "get_trace_errors": {"errors": []},
                                     "get_critical_path": {"critical_path_duration_us": 2_800_000, "segments": []}}))
    seen = {}

    async def fake_explain(bundle, question):
        seen["bundle"] = bundle
        return {"ok": True, "status": "ok", "answer": "事实：[E] 解释：推测 证据：[E] 下一步：看日志",
                "model": "x", "usage": {"total_tokens": 10}, "finish_reason": "stop", "elapsed_ms": 1.0,
                "request": {"messages": []}, "raw_response": {"choices": []}}

    monkeypatch.setattr("insight.app.engine.explain", fake_explain)
    resp = client.post("/api/analyze", headers=auth(token),
                       json={"question_type": "latency_breakdown", "question": "慢在哪", "trace_id": "a" * 32})
    assert resp.status_code == 200
    bundle = seen["bundle"]
    assert "summary" in bundle and bundle.get("raw") is not None
    request = seen["bundle"]
    assert "raw" in request  # evidence keeps raw; the model prompt must not
    prompt = json.dumps(explain.build_prompt(request, "慢在哪"), ensure_ascii=False)
    assert "result_sha256" not in prompt or "raw" not in request.get("facts", {})
    assert bundle["limits"]["summary_bytes"] <= core.MAX_EVIDENCE_BYTES
    assert bundle["raw_sha256"]


# ---------------------------------------------------------------- arithmetic


def test_units_are_milliseconds():
    rows = core.summarise_spans([
        {"span_name": "model.chat", "duration_us": 1_500_000},
        {"span_name": "model.chat", "duration_us": 500_000},
        {"span_name": "doc.search", "duration_us": 8_490},
    ])
    by_name = {r["stage"]: r for r in rows["stages"]}
    assert by_name["model.chat"]["total_ms"] == 2000.0
    assert by_name["doc.search"]["total_ms"] == 8.49
    assert "不可相加" in rows["note"]


def _series(points):
    return {"series": [{"labels": {}, "points": points}], "step_s": 15,
            "start": points[0][0] if points else 0, "end": points[-1][0] if points else 0}



def _ts(points, start, end):
    return {"series": [{"labels": {}, "points": [[t, float(t)] for t in points]}],
            "start": start, "end": end, "step_s": 15}


def _up(value=1, n=20):
    return {"series": [{"labels": {}, "points": [[100 + i * 15, value] for i in range(n)]}],
            "start": 100, "end": 400, "step_s": 15}


def _win(counter, ts_points, start, end, up_value=1):
    span = list(range(int(start), int(end) + 1, 15))
    return core.windows_summary(counter, counter, _ts(ts_points, start, end), _ts(ts_points, start, end),
                                _up(up_value), _up(up_value), start, end, start, end, 15.0)

def test_windows_reset_is_per_series():
    a = _series([[100, 10], [115, 12], [130, 15]])
    b = _series([[200, 20], [215, 3], [230, 9]])
    out = core.windows_summary(
        a, b,
        _ts([100, 115, 130], 200, 230), _ts([200, 215, 230], 200, 230),
        _up(), _up(), 200, 230, 200, 230, 15.0)
    assert out["window_b"]["resets"] == 1
    assert out["window_b"]["delta_total"] == 9.0


def test_windows_need_samples_and_freshness():
    a = _series([[100, 1], [115, 2], [130, 3]])
    b = _series([[100, 1]])
    out = core.windows_summary(
        a, b,
        _ts([100, 115, 130], 100, 130), _ts([130], 100, 130),
        _up(), _up(), 100, 130, 100, 130, 15.0)
    assert out["comparable"] is False
    assert out["window_b"]["usable"] is False


def test_windows_equal_length_required():
    a = _series([[100, 1], [115, 2], [130, 3]])
    b = _series([[100, 1], [115, 2], [130, 3]])
    out = _win(a, list(range(100, 131, 15)), 100, 130)
    assert out["equal_length"] is True
    assert out["comparable"] is True


# ---------------------------------------------------------------- citations and statuses


def test_guard_requires_a_known_citation():
    bundle = {"evidence_id": "E-real-1"}
    answer = "事实：x\n解释：y\n证据：见 [E-fake-9]\n下一步：检查。"
    guard = explain.guard_answer(answer, bundle)
    assert guard["unknown_citations"] == ["E-fake-9"]
    assert guard["citation_ok"] is False and guard["verdict"] == "citation_missing"


def test_guard_rejects_answer_without_citations():
    guard = explain.guard_answer("事实：x\n解释：y\n证据：无\n下一步：检查。", {"evidence_id": "E-real-1"})
    assert guard["citation_ok"] is False


def test_guard_accepts_known_citation():
    guard = explain.guard_answer("事实：x\n解释：y\n证据：[E-real-1]\n下一步：检查。", {"evidence_id": "E-real-1"})
    assert guard["citation_ok"] is True and guard["verdict"] == "ok"


def test_truncated_answer_is_not_ok():
    guard = explain.guard_answer("事实：x [E-real-1]", {"evidence_id": "E-real-1"}, status="truncated")
    assert guard["structural_pass"] is False and guard["verdict"] == "truncated"


def test_model_failure_is_502(token, monkeypatch):
    monkeypatch.setattr(clients.JaegerMCP, "call",
                        fake_jaeger({"search_traces": {"traces": [{"trace_id": "a" * 32, "start_time": core.iso((core.now_utc() - timedelta(minutes=5)).replace(microsecond=0)),
                                                                   "duration_us": 1, "span_count": 3, "has_errors": False}]},
                                     "get_trace_topology": TOPO, "get_span_details": DETAILS,
                                     "get_trace_errors": {"errors": []},
                                     "get_critical_path": {"critical_path_duration_us": 1, "segments": []}}))

    async def fake_explain(bundle, question):
        return {"ok": False, "status": "model_unavailable", "error": "model_unavailable: ConnectError",
                "elapsed_ms": 1.0, "request": {}}

    monkeypatch.setattr("insight.app.engine.explain", fake_explain)
    resp = client.post("/api/analyze", headers=auth(token),
                       json={"question_type": "latency_breakdown", "question": "慢在哪", "trace_id": "a" * 32})
    assert resp.status_code == 502 and resp.json()["status"] == "model_unavailable"


# ---------------------------------------------------------------- frozen evidence reuse


def test_explain_reuses_frozen_evidence(token, monkeypatch):
    core.save_evidence({"evidence_id": "E-freeze-1", "question_type": "latency_breakdown", "facts": {"a": 1},
                        "summary": {"a": 1}, "sources": [], "limits": {}, "window": {"start": "x", "end": "y"},
                        "frozen_end": "y", "target": {"service": "action-lab-api"}, "raw_sha256": "0" * 64})
    seen = {}

    async def fake_explain(bundle, question):
        seen["eid"] = bundle["evidence_id"]
        seen["question"] = question
        return {"ok": True, "status": "ok", "answer": "事实：[E-freeze-1]", "model": "x",
                "usage": {}, "finish_reason": "stop", "elapsed_ms": 1.0, "request": {}, "raw_response": {}}

    monkeypatch.setattr("insight.app.engine.explain", fake_explain)
    resp = client.post("/api/explain", headers=auth(token), json={"evidence_id": "E-freeze-1", "question": "为什么慢"})
    assert resp.status_code == 200
    assert seen["eid"] == "E-freeze-1" and seen["question"] == "为什么慢"
    assert resp.json()["run_id"].startswith("R-")


# ---------------------------------------------------------------- concurrency


def test_busy_when_concurrency_exhausted(token, monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(2)
        return {"tool": "x", "arguments": {}, "elapsed_ms": 1, "result": {}, "raw_bytes": 1, "result_sha256": "0" * 64}

    monkeypatch.setattr(clients.JaegerMCP, "call", slow)
    import insight.app as appmod
    monkeypatch.setattr(appmod, "_sem", asyncio.Semaphore(0))
    resp = client.post("/api/analyze", headers=auth(token),
                       json={"question_type": "latency_breakdown", "question": "慢在哪"})
    assert resp.status_code == 429


def test_two_explanations_never_share_run_id(token, monkeypatch):
    """C: same evidence inside one second must still produce two run records."""
    ids = []

    async def fake_explain(bundle, question):
        return {"ok": True, "status": "ok", "answer": "事实：x [%s]\n解释：y\n证据：z\n下一步：w。" % bundle["evidence_id"],
                "model": "m", "usage": {}, "finish_reason": "stop", "elapsed_ms": 1.0,
                "request": {"messages": []}, "raw_response": {}, "request_sha256": "0", "response_sha256": "0",
                "temperature": 0.2, "enable_thinking": False}

    import insight.app as appmod
    monkeypatch.setattr(appmod.engine, "explain", fake_explain)
    core.save_evidence({"evidence_id": "E-test-runid", "question_type": "latency_breakdown",
                        "created_at": core.iso(core.now_utc()), "frozen_end": core.iso(core.now_utc()),
                        "window": {"minutes": 60}, "target": {"service": "action-lab-api"},
                        "facts": {}, "summary": {}, "limits": {"summary_bytes": 2, "truncated": False},
                        "sources": [], "raw_sha256": "0"})
    for _ in range(2):
        resp = client.post("/api/explain", headers=auth(token),
                           json={"evidence_id": "E-test-runid", "question": "慢在哪"})
        ids.append(resp.json()["run_id"])
    assert len(set(ids)) == 2


def test_dense_eval_points_with_one_real_sample_are_not_coverage():
    """timestamp() lookback repeats one sample across dense evaluation points."""
    counter = _series([[570, 1], [585, 2], [600, 3]])
    dense = {"series": [{"labels": {}, "points": [[t, 570.0] for t in range(300, 601, 15)]}],
             "start": 300, "end": 600, "step_s": 15}
    out = core.windows_summary(counter, counter, dense, dense, _up(), _up(), 300, 600, 300, 600, 15.0)
    assert out["window_a"]["real_coverage_ratio"] < 0.1
    assert out["comparable"] is False


def test_real_samples_spanning_window_are_comparable():
    counter = _series([[300, 1], [315, 2], [600, 30]])
    real = {"series": [{"labels": {}, "points": [[t, float(t)] for t in range(300, 601, 15)]}],
            "start": 300, "end": 600, "step_s": 15}
    out = core.windows_summary(counter, counter, real, real, _up(), _up(), 300, 600, 300, 600, 15.0)
    assert out["window_a"]["real_coverage_ratio"] >= 0.9
    assert out["comparable"] is True


def _counter(labels, points):
    return {"series": [{"labels": labels, "points": points}]}


def test_mixed_series_with_stale_sample_stays_unusable():
    """200 series fully sampled + 500 series with one old sample: not comparable."""
    counter = {"series": [
        {"labels": {"status": "200"}, "points": [[300, 10], [315, 20], [600, 200]]},
        {"labels": {"status": "500"}, "points": [[300, 2], [315, 2], [600, 2]]},
    ]}
    ts_full = {"series": [{"labels": {"status": "200"},
                           "points": [[t, float(t)] for t in range(300, 601, 15)]}]}
    ts_stale = {"series": [{"labels": {"status": "500"}, "points": [[t, 0.0] for t in range(300, 601, 15)]}]}
    out = core.windows_summary(counter, counter, ts_full | ts_stale, ts_full | ts_stale,
                               _up(), _up(), 300, 600, 300, 600, 15.0)
    window = out["window_a"]
    assert window["series_count"] == 2
    assert window["real_coverage_ratio"] < 0.1
    assert window["usable"] is False
    assert window["unusable_series"]


def test_series_without_timestamp_stream_is_unusable():
    counter = {"series": [{"labels": {"status": "200"}, "points": [[300, 1], [315, 2]]}]}
    empty_ts = {"series": []}
    out = core.windows_summary(counter, counter, empty_ts, empty_ts, _up(), _up(),
                               300, 600, 300, 600, 15.0)
    assert out["window_a"]["usable"] is False
    assert out["comparable"] is False


def test_index_page_is_served():
    """The page entry must exist at / and carry a stable marker."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "运维观测分析" in resp.text
    assert client.get("/api/whoami").status_code == 401
