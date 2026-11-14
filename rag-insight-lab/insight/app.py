"""Read-only observability analysis API (article 07 lab)."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import clients, core, explain

app = FastAPI(title="rag-insight-lab", version="1.1.0")
jaeger = clients.JaegerMCP()
prom = clients.Prometheus()
engine = explain.Explainer()
semaphore = asyncio.Semaphore(core.MAX_CONCURRENCY)
TRACE_RE = re.compile(r"^[0-9a-f]{16,32}$")


def require_identity(request: Request) -> core.Identity:
    ident = core.identity_from_header(request.headers.get("authorization"))
    if ident is None:
        raise HTTPException(status_code=401, detail="需要运维实验凭据")
    if ident.role != "ops":
        raise HTTPException(status_code=403, detail="该凭据没有运维分析权限")
    return ident


class AnalyzeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_type: str = Field(min_length=3, max_length=40)
    question: str = Field(min_length=2, max_length=300)
    service: str = Field(default="action-lab-api", min_length=1, max_length=80)
    trace_id: str | None = Field(default=None, max_length=32)
    window_minutes: int = Field(default=60, ge=5, le=180)


class ExplainBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(min_length=8, max_length=80)
    question: str = Field(min_length=2, max_length=300)


def frozen_end() -> datetime:
    return (datetime.now(timezone.utc) - timedelta(seconds=45)).replace(second=0, microsecond=0)


def _validate_scope(body: AnalyzeBody) -> None:
    if body.service not in core.ALLOWED_SERVICES:
        raise HTTPException(status_code=403, detail="服务不在允许范围内")
    if body.question_type == "window_requests" and body.service != core.COUNTER_SERVICE:
        raise HTTPException(status_code=403, detail="计数窗口只对应被采集的 API 服务")
    if body.trace_id is not None and not TRACE_RE.match(body.trace_id):
        raise HTTPException(status_code=400, detail="Trace 编号格式不正确")


def _span_id_from_path(path: str) -> str | None:
    """Jaeger v2 TopologySpan has no span_id; the last path segment is it."""
    if not path:
        return None
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return last if re.match(r"^[0-9a-f]{8,32}$", last) else None


def _path_depth(path: str) -> int:
    return len([p for p in (path or "").split("/") if p])


async def _span_details(spans: list[dict], trace_id: str, sources: list[dict]) -> tuple[list[dict], list[str]]:
    """Whitelisted attributes for the root and tool spans (ids derived from path)."""
    wanted: list[str] = []
    mapping: dict[str, dict] = {}
    for span in spans:
        sid = _span_id_from_path(span.get("path", ""))
        name = span.get("span_name", "")
        if not sid:
            continue
        mapping[sid] = span
        if _path_depth(span.get("path", "")) <= 1 or name.startswith(("tool.", "doc.", "mcp.")):
            wanted.append(sid)
    wanted = wanted[:8]
    if not wanted:
        return [], []
    detail = await jaeger.call("get_span_details", {"trace_id": trace_id, "span_ids": wanted})
    sources.append(detail)
    rows = detail["result"].get("spans") or detail["result"].get("details") or []
    clean = core.sanitise({"spans": rows})["spans"]
    for row in clean:
        src = mapping.get(row.get("span_id", ""))
        if src and not row.get("span_name"):
            row["span_name"] = src.get("span_name")
    return clean, wanted


def _http_and_business(details: list[dict], cover_seen: list[str], root_id: str | None = None) -> dict:
    http_status = None
    outcomes: dict[str, str] = {}
    errors: list[str] = []
    for row in details:
        attrs = row.get("attributes") or {}
        name = row.get("span_name", "")
        code = attrs.get("http.status_code") or attrs.get("http.response.status_code")
        is_root = row.get("span_id") == root_id or (root_id is None and not row.get("parent_span_id"))
        if code and http_status is None and is_root:
            http_status = code
        if name.startswith(("tool.", "doc.", "mcp.")):
            outcome = attrs.get("lab.outcome")
            if outcome:
                outcomes[name] = str(outcome)
            kind = attrs.get("lab.error_kind")
            if kind:
                errors.append(str(kind))
    covered = bool(details)
    return {
        "http_status": http_status,
        "tool_outcomes": outcomes,
        "error_kinds": sorted(set(errors)),
        "details_requested": len(cover_seen),
        "details_returned": len(details),
        "evidence_complete": covered and len(details) >= len(cover_seen),
    }


async def _bundle_trace(question_type: str, body: AnalyzeBody, minutes: int) -> dict:
    end = frozen_end()
    start = end - timedelta(minutes=minutes)
    sources: list[dict] = []
    trace_id = body.trace_id
    if trace_id:
        topo = await jaeger.call("get_trace_topology", {"trace_id": trace_id})
        sources.append(topo)
        spans = topo["result"].get("spans", [])
        services = {s.get("service") for s in spans}
        if body.service not in services:
            raise HTTPException(status_code=403, detail="该 Trace 不属于允许的服务范围")
        starts = [s.get("start_time") for s in spans if s.get("start_time")]
        if not starts:
            raise HTTPException(status_code=404, detail="Trace 不存在或已过期")
        ts = datetime.fromisoformat(min(starts).replace("Z", "+00:00"))
        if not (start <= ts <= end):
            raise HTTPException(status_code=403, detail="Trace 不在允许的时间窗口内")
        summary = {
            "trace_id": trace_id,
            "root_span_name": spans[0].get("span_name"),
            "services": sorted(x for x in services if x),
            "span_count": len(spans),
            "duration_us": spans[0].get("duration_us"),
            "has_errors": any(s.get("status") == "Error" for s in spans),
            "start_time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    else:
        span_name = "POST /api/ask" if question_type in ("latency_breakdown", "capacity_gap") else None
        filters = {
            "service_name": body.service,
            "start_time_min": core.iso(start),
            "start_time_max": core.iso(end),
        }
        if span_name:
            filters["span_name"] = span_name
        if question_type == "failure_where":
            filters["with_errors"] = True
        found = await jaeger.call("search_traces", filters)
        sources.append(found)
        traces = found["result"].get("traces", [])
        if not traces and not span_name:
            found = await jaeger.call(
                "search_traces",
                {"service_name": body.service, "start_time_min": core.iso(start), "start_time_max": core.iso(end)},
            )
            sources.append(found)
            traces = found["result"].get("traces", [])
        if not traces:
            raise HTTPException(status_code=404, detail="窗口内没有可分析的 Trace")
        traces.sort(key=lambda t: t.get("start_time", ""), reverse=True)
        summary = traces[0]
        trace_id = summary["trace_id"]
        topo = await jaeger.call("get_trace_topology", {"trace_id": trace_id})
        sources.append(topo)
        spans = topo["result"].get("spans", [])

    details, requested = await _span_details(spans, trace_id, sources)
    root_id = _span_id_from_path(spans[0].get("path", "")) if spans else None
    coverage = _http_and_business(details, requested, root_id)
    facts: dict = {
        "trace": core.summarise_trace_summary(summary),
        "stages": core.summarise_spans(spans),
        "http_and_business": coverage,
        "span_names_seen": sorted({s.get("span_name", "") for s in spans})[:30],
    }
    if question_type in ("failure_where", "latency_breakdown"):
        try:
            errs = await jaeger.call("get_trace_errors", {"trace_id": trace_id})
            sources.append(errs)
            facts["error_spans"] = core.sanitise(errs["result"])
        except clients.BackendError as exc:
            facts["error_spans"] = {"status": "unavailable", "detail": str(exc)}
    if question_type in ("failure_where", "latency_breakdown"):
        try:
            crit = await jaeger.call("get_critical_path", {"trace_id": trace_id})
            sources.append(crit)
            crit_data = core.sanitise(crit["result"])
            facts["critical_path"] = {
                "duration_ms": crit_data.get("critical_path_duration_ms"),
                "segments": len(crit_data.get("segments", []) or []),
                "note": "按 section 给出；同一 Span 可重复出现，不能相加当作总耗时。",
            }
        except clients.BackendError as exc:
            facts["critical_path"] = {"status": "unavailable", "detail": str(exc)}
    if question_type == "failure_where":
        facts["http_vs_business_note"] = (
            "HTTP 状态取自根 Span 属性；no_record/rejected 等属于业务结果，不是系统故障。"
        )
    if question_type == "capacity_gap":
        facts["gpu_internal"] = {
            "available": False,
            "note": "Span 只覆盖应用与工具调用；没有 GPU 排队、prefill、decode 等服务端指标。",
        }
    return {
        "evidence_id": core.new_evidence_id(question_type),
        "question_type": question_type,
        "created_at": core.iso(core.now_utc()),
        "frozen_end": core.iso(end),
        "window": {"start": core.iso(start), "end": core.iso(end), "minutes": minutes},
        "target": {"service": body.service, "trace_id": trace_id},
        "facts": facts,
        "sources": [
            {"name": f"jaeger_mcp.{s['tool']}", "arguments": s["arguments"], "sha256": s["result_sha256"],
             "elapsed_ms": s["elapsed_ms"], "bytes": s["raw_bytes"]}
            for s in sources
        ],
        "raw": sources,
    }


async def _bundle_windows(body: AnalyzeBody) -> dict:
    end = frozen_end()
    minutes = body.window_minutes
    span = timedelta(minutes=minutes)
    a_start, a_end = end - 2 * span, end - span
    b_start, b_end = end - span, end
    step = 15
    raw_counter = core.COUNTER_METRIC
    sources: list[dict] = []

    async def q(expr: str, start: datetime, stop: datetime) -> dict:
        try:
            out = await prom.query_range(expr, start.timestamp(), stop.timestamp(), step)
        except clients.BackendError as exc:
            raise HTTPException(status_code=503, detail=f"指标后端不可用：{exc}") from exc
        sources.append({"name": "prometheus.query_range",
                        "arguments": {"expr": out["expr"], "start": out["start"], "end": out["end"], "step": out["step_s"]},
                        "sha256": out["result_sha256"], "elapsed_ms": 0, "bytes": 0})
        return out

    a = await q(raw_counter, a_start, a_end)
    b = await q(raw_counter, b_start, b_end)
    a_ts = await q(f"timestamp({raw_counter})", a_start, a_end)
    b_ts = await q(f"timestamp({raw_counter})", b_start, b_end)
    a_up = await q("up", a_start, a_end)
    b_up = await q("up", b_start, b_end)
    facts = {
        "metric": raw_counter,
        "service": core.COUNTER_SERVICE,
        "windows": core.windows_summary(a, b, a_ts, b_ts, a_up, b_up,
                                        a_start.timestamp(), a_end.timestamp(),
                                        b_start.timestamp(), b_end.timestamp(), float(step)),
    }
    return {
        "evidence_id": core.new_evidence_id("window"),
        "question_type": "window_requests",
        "created_at": core.iso(core.now_utc()),
        "frozen_end": core.iso(end),
        "window": {"a": [core.iso(a_start), core.iso(a_end)], "b": [core.iso(b_start), core.iso(b_end)],
                   "minutes": minutes, "step_s": step},
        "target": {"service": core.COUNTER_SERVICE, "metric": raw_counter},
        "facts": facts,
        "sources": sources,
        "raw": {"counter_a": a, "counter_b": b, "timestamp_a": a_ts, "timestamp_b": b_ts,
                "up_a": a_up, "up_b": b_up},
    }



_sem = asyncio.Semaphore(core.MAX_CONCURRENCY)


async def _bounded(work):
    """Bounded concurrency plus a wall-clock budget for one request."""
    if _sem.locked():
        raise HTTPException(status_code=429, detail="分析并发已满，请稍后重试")
    async with _sem:
        try:
            return await asyncio.wait_for(work(), timeout=core.REQUEST_BUDGET)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="分析超时") from exc


def _finalize(bundle: dict) -> dict:
    """Collection step: compute the model-facing summary and persist evidence once."""
    summary, truncated = core.fit(bundle["facts"])
    bundle["summary"] = summary["data"]
    bundle["limits"] = {"max_evidence_bytes": core.MAX_EVIDENCE_BYTES, "summary_bytes": summary["bytes"],
                        "truncated": truncated}
    raw = bundle.get("raw")
    raw_text = json.dumps(raw if raw is not None else None, ensure_ascii=False, sort_keys=True)
    bundle["raw_sha256"] = core.sha256_text(raw_text)
    core.save_evidence(bundle)
    core.save_raw(bundle["evidence_id"], raw if raw is not None else None)
    return bundle


async def _explain(bundle: dict, question: str, ident: core.Identity) -> JSONResponse:
    """Explanation step: read-only use of a frozen evidence bundle."""
    started = time.monotonic()
    result = await engine.explain(bundle, question)
    answer = result.get("answer") or ""
    guard = explain.guard_answer(answer, bundle, result.get("status", "ok")) if answer else None
    run_id = f"R-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:12]}"
    record = {
        "run_id": run_id,
        "ts": core.iso(core.now_utc()),
        "user": ident.name,
        "evidence_id": bundle["evidence_id"],
        "evidence_sha256": core.sha256_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True)),
        "question": question,
        "service": (bundle.get("target") or {}).get("service"),
        "answer": answer,
        "answer_guard": guard,
        "model_request": result.get("request"),
        "model_response": result.get("raw_response"),
        "model": {
            "name": result.get("model"),
            "temperature": result.get("temperature"),
            "enable_thinking": result.get("enable_thinking"),
            "usage": result.get("usage"),
            "finish_reason": result.get("finish_reason"),
            "status": result.get("status"),
            "elapsed_ms": result.get("elapsed_ms"),
        },
        "request_sha256": result.get("request_sha256"),
        "response_sha256": result.get("response_sha256"),
    }
    core.write_json(core.RUN_DIR / f"{bundle['evidence_id']}-{run_id}.json", record)
    status = result.get("status", "ok")
    http_status = 200
    if status in ("model_unavailable", "model_http_error"):
        http_status = 502
    return JSONResponse(
        status_code=http_status,
        content={
            "status": status,
            "run_id": run_id,
            "evidence_id": bundle["evidence_id"],
            "window": bundle.get("window"),
            "facts": bundle.get("summary"),
            "sources": bundle.get("sources"),
            "limits": bundle.get("limits"),
            "raw_sha256": bundle.get("raw_sha256"),
            "answer": answer,
            "answer_guard": guard,
            "model": record["model"],
            "error": result.get("error"),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )



@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Read-only analysis page. Served from the fixed path, never from user input."""
    page = core.ROOT / "web" / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=500, detail="页面文件缺失")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "rag-insight-lab"}


@app.get("/api/whoami")
async def whoami(ident: core.Identity = Depends(require_identity)) -> dict:
    return {"status": "ok", "name": ident.name, "role": ident.role}


@app.post("/api/analyze")
async def analyze(body: AnalyzeBody, ident: core.Identity = Depends(require_identity)) -> JSONResponse:
    if body.question_type not in core.QUESTION_TYPES:
        raise HTTPException(status_code=400, detail="未知的问题类型")
    _validate_scope(body)

    async def work():
        try:
            bundle = (await _bundle_windows(body)) if body.question_type == "window_requests" \
                else (await _bundle_trace(body.question_type, body, body.window_minutes))
        except clients.BackendError as exc:
            return JSONResponse(status_code=503, content={"status": "backend_unavailable", "detail": str(exc)})
        _finalize(bundle)
        return await _explain(bundle, body.question, ident)

    return await _bounded(work)


@app.post("/api/explain")
async def explain_existing(body: ExplainBody, ident: core.Identity = Depends(require_identity)) -> JSONResponse:
    bundle = core.load_evidence(body.evidence_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="证据不存在")
    return await _bounded(lambda: _explain(bundle, body.question, ident))


@app.get("/api/evidence/{eid}")
async def evidence(eid: str, ident: core.Identity = Depends(require_identity)) -> dict:
    bundle = core.load_evidence(eid)
    if bundle is None:
        raise HTTPException(status_code=404, detail="证据不存在")
    return bundle
