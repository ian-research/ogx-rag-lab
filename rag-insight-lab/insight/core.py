"""Core settings, private config, evidence store and program-side computation."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value else default


STATE_DIR = Path(_env("STATE_DIR", str(ROOT.parent / "state")))
DATA_DIR = Path(_env("DATA_DIR", str(STATE_DIR / "data")))
PRIVATE_DIR = Path(_env("PRIVATE_DIR", str(STATE_DIR / "private")))
EVIDENCE_DIR = Path(_env("EVIDENCE_DIR", str(STATE_DIR / "evidence")))
RAW_DIR = Path(_env("RAW_DIR", str(STATE_DIR / "evidence-raw")))
RUN_DIR = Path(_env("RUN_DIR", str(STATE_DIR / "runs")))

API_PORT = int(_env("API_PORT", "8096"))
JAEGER_MCP_URL = _env("JAEGER_MCP_URL", "http://127.0.0.1:16686/api/ai/mcp/")
JAEGER_QUERY_URL = _env("JAEGER_QUERY_URL", "http://127.0.0.1:16686")
PROM_URL = _env("PROM_URL", "http://127.0.0.1:9090")
OGX_BASE = _env("OGX_BASE", "http://127.0.0.1:8321")
OGX_MODEL = _env("OGX_MODEL", "local-vllm/rag-llm")
MODEL_TIMEOUT = float(_env("MODEL_TIMEOUT", "120"))
TOOL_TIMEOUT = float(_env("TOOL_TIMEOUT", "30"))
REQUEST_BUDGET = float(_env("REQUEST_BUDGET", "90"))
MAX_CONCURRENCY = int(_env("MAX_CONCURRENCY", "2"))
MAX_EVIDENCE_BYTES = int(_env("MAX_EVIDENCE_BYTES", "24000"))
MAX_TOOL_BYTES = int(_env("MAX_TOOL_BYTES", "120000"))
DEFAULT_WINDOW_MINUTES = int(_env("DEFAULT_WINDOW_MINUTES", "60"))
ALLOWED_SERVICES = [
    s.strip()
    for s in _env("ALLOWED_SERVICES", "action-lab-api,action-lab-mcp").split(",")
    if s.strip()
]
COUNTER_METRIC = _env("COUNTER_METRIC", "actionlab_http_requests_total")
COUNTER_SERVICE = _env("COUNTER_SERVICE", "action-lab-api")

QUESTION_TYPES = {
    "latency_breakdown": "这次问答主要等在模型接口，还是文档或工具查询？",
    "failure_where": "这次失败发生在哪一环？HTTP 成功就代表业务成功吗？",
    "window_requests": "两个等长窗口的请求数有什么不同？",
    "capacity_gap": "这次慢是不是 GPU 不足造成的？",
}

# Attributes we are willing to show: no bodies, no confirmation values, no prompts.
SPAN_ATTR_WHITELIST = {
    "http.status_code",
    "http.request.method",
    "http.route",
    "otel.status_code",
    "otel.status_description",
    "lab.outcome",
    "lab.tool",
    "lab.error_kind",
    "lab.as_of",
    "lab.wants_draft",
    "error.kind",
}
ATTRIBUTE_ALLOWED = ("trace_id", "span_id", "service", "span_name", "duration_ms", "status", "attributes")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def private_text(path: Path) -> str:
    st = path.stat()
    if st.st_mode & 0o077:
        raise PermissionError(f"private file must be 0600: {path}")
    return path.read_text(encoding="utf-8")


class Identity:
    def __init__(self, name: str, role: str):
        self.name = name
        self.role = role


def load_admins() -> dict[str, dict]:
    path = PRIVATE_DIR / "admins.json"
    if not path.exists():
        return {}
    return json.loads(private_text(path))


def identity_from_header(authorization: str | None) -> Identity | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer ") :].strip()
    if not (32 <= len(token) <= 200):
        return None
    digest = sha256_text(token)
    for name, row in load_admins().items():
        if secrets.compare_digest(row.get("sha256", ""), digest):
            return Identity(name=name, role=row.get("role", "ops"))
    return None


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def new_evidence_id(kind: str) -> str:
    return f"E-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{kind[:4]}-{secrets.token_hex(2)}"


def save_evidence(bundle: dict) -> str:
    """Evidence is immutable: an existing id is never overwritten."""
    eid = bundle["evidence_id"]
    path = EVIDENCE_DIR / f"{eid}.json"
    if path.exists():
        raise FileExistsError(f"evidence {eid} already exists")
    write_json(path, bundle)
    return eid


def save_raw(eid: str, payload: dict) -> None:
    """Raw tool returns are written exactly once, at collection time."""
    path = RAW_DIR / f"{eid}.json"
    if path.exists():
        return
    write_json(path, payload)


def load_raw(eid: str) -> dict | None:
    path = RAW_DIR / f"{eid}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_evidence(eid: str) -> dict | None:
    if not eid or "/" in eid or ".." in eid:
        return None
    path = EVIDENCE_DIR / f"{eid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- computation


def _ms(us: int | float | None) -> float | None:
    if us is None:
        return None
    return round(float(us) / 1000.0, 3)


STAGE_NAMES = (
    "model.chat",
    "model.loop",
    "doc.search",
    "tool.search_documents",
    "tool.get_employee_laptop_info",
    "tool.note_user_reason",
    "draft.build",
    "draft.confirm",
    "draft.submit",
    "mcp.call",
    "POST /mcp",
)


def summarise_spans(spans: list[dict]) -> dict:
    """Aggregate topology spans (microseconds) into non-additive stage rows."""
    rows: dict[str, dict] = {}
    for span in spans:
        name = span.get("span_name", "")
        if name not in STAGE_NAMES:
            continue
        row = rows.setdefault(name, {"stage": name, "count": 0, "total_ms": 0.0, "max_ms": 0.0})
        row["count"] += 1
        dur_ms = _ms(span.get("duration_us")) or 0.0
        row["total_ms"] = round(row["total_ms"] + dur_ms, 3)
        row["max_ms"] = max(row["max_ms"], dur_ms)
    return {
        "stages": sorted(rows.values(), key=lambda r: r["total_ms"], reverse=True),
        "note": "同一阶段可能包含并行或嵌套 Span；各行不可相加。model.chat 是客户端往返，不含 GPU 内部阶段。",
    }


def summarise_trace_summary(summary: dict) -> dict:
    return {
        "trace_id": summary.get("trace_id"),
        "root_span": summary.get("root_span_name"),
        "services": summary.get("services", []),
        "span_count": summary.get("span_count"),
        "duration_ms": _ms(summary.get("duration_us")),
        "has_errors": bool(summary.get("has_errors")),
        "start_time": summary.get("start_time"),
    }


def _series_stats(points: list[list], step_s: float, window_end: float) -> dict:
    """Reset-aware delta and coverage for one counter series."""
    if not points:
        return {"points": 0, "delta": None, "resets": 0, "first_ts": None, "last_ts": None,
                "coverage_s": 0.0, "max_gap_s": None, "freshness_s": None}
    points = sorted(points, key=lambda p: p[0])
    deltas, resets = 0.0, 0
    prev = None
    for ts, val in points:
        if prev is not None:
            if val >= prev:
                deltas += val - prev
            else:
                resets += 1
                deltas += val  # counter restarted from zero
        prev = val
    ts_list = [p[0] for p in points]
    gaps = [round(ts_list[i] - ts_list[i - 1], 1) for i in range(1, len(ts_list))]
    return {
        "points": len(points),
        "delta": round(deltas, 3),
        "resets": resets,
        "first_ts": ts_list[0],
        "last_ts": ts_list[-1],
        "coverage_s": round(ts_list[-1] - ts_list[0], 1),
        "max_gap_s": max(gaps) if gaps else None,
        "freshness_s": round(window_end - ts_list[-1], 1),
        "step_s": step_s,
    }


def _window_block(rng: dict, rng_ts: dict, rng_up: dict, start: float, end: float, step_s: float) -> dict:
    stats = [_series_stats(item["points"], step_s, end) for item in rng.get("series", [])]
    total = sum(st["delta"] or 0 for st in stats) if stats else None
    # Real sample coverage comes from timestamp(counter). Match by labels (ignoring
    # __name__), never by position: a series without timestamps or with a single
    # sample stays in the result as unusable instead of being dropped.
    def _key(labels: dict) -> tuple:
        return tuple(sorted((k, str(v)) for k, v in (labels or {}).items() if k != "__name__"))

    ts_by_key: dict[tuple, list[float]] = {}
    for item in rng_ts.get("series", []):
        key = _key(item.get("labels") or {})
        values = ts_by_key.setdefault(key, [])
        for pt in item.get("points", []):
            if len(pt) > 1:
                values.append(round(float(pt[1]), 3))

    real: list[dict] = []
    for item in rng.get("series", []):
        key = _key(item.get("labels") or {})
        ts = sorted(set(ts_by_key.get(key, [])))
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        if len(ts) >= 2:
            covered = sum(g for g in gaps if g <= 3 * step_s)
            real.append({
                "labels": dict(key),
                "points": len(ts),
                "first_gap_s": round(ts[0] - start, 1),
                "last_gap_s": round(end - ts[-1], 1),
                "covered_s": round(covered, 1),
                "coverage_ratio": round(covered / (end - start), 3) if end > start else 0.0,
                "max_gap_s": round(max(gaps), 1),
            })
        else:
            real.append({
                "labels": dict(key), "points": len(ts), "first_gap_s": None,
                "last_gap_s": None, "covered_s": 0.0, "coverage_ratio": 0.0,
                "max_gap_s": None, "reason": "missing_timestamp_series" if not ts else "single_sample",
            })
    up_vals = [pt[1] for item in rng_up.get("series", []) for pt in item.get("points", [])]
    up_ratio = round(sum(1 for v in up_vals if v >= 1) / len(up_vals), 3) if up_vals else None
    worst = min(real, key=lambda r: r["coverage_ratio"]) if real else None
    full_cover = bool(real) and all(
        r["coverage_ratio"] >= 0.9
        and r["first_gap_s"] is not None and r["first_gap_s"] <= 2 * step_s
        and r["last_gap_s"] is not None and r["last_gap_s"] <= 2 * step_s
        for r in real
    )
    return {
        "series_count": len(stats),
        "points_per_series": [st["points"] for st in stats],
        "delta_total": round(total, 3) if total is not None else None,
        "resets": sum(st["resets"] for st in stats),
        "eval_points": sum(st["points"] for st in stats),
        "real_samples": real,
        "real_coverage_ratio": round(worst["coverage_ratio"], 3) if worst else 0.0,
        "real_max_gap_s": max((r["max_gap_s"] or 0 for r in real), default=0.0),
        "first_gap_s": worst["first_gap_s"] if worst else None,
        "last_gap_s": worst["last_gap_s"] if worst else None,
        "unusable_series": [r["labels"] for r in real if r.get("reason")],
        "up_ratio": up_ratio,
        "usable": full_cover and (up_ratio is None or up_ratio >= 0.9),
    }


def windows_summary(range_a: dict, range_b: dict, ts_a: dict, ts_b: dict, up_a: dict, up_b: dict,
                    a_start: float, a_end: float, b_start: float, b_end: float, step_s: float) -> dict:
    a = _window_block(range_a, ts_a, up_a, a_start, a_end, step_s)
    b = _window_block(range_b, ts_b, up_b, b_start, b_end, step_s)
    equal_length = abs((a_end - a_start) - (b_end - b_start)) < 1e-6
    return {
        "window_a": a,
        "window_b": b,
        "equal_length": equal_length,
        "comparable": bool(equal_length and a["usable"] and b["usable"]),
        "rule": "可比需两窗都满足：真实样本覆盖≥90%、首尾缺口≤2×step、up≥90%；否则只报事实不比较。",
        "note": "delta 为逐 series 去重置后累加；求值点不等于抓取次数，覆盖与缺口用 timestamp() 的真实样本时间。",
    }


def _redact(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key.lower().endswith("_us") and isinstance(item, (int, float)):
                out[key[:-3] + "_ms"] = round(item / 1000.0, 3)
            elif key in ("events", "links", "logs"):
                continue
            elif key == "attributes" and isinstance(item, dict):
                out[key] = {k: v for k, v in item.items() if k in SPAN_ATTR_WHITELIST}
            elif isinstance(item, str) and len(item) > 200:
                out[key] = item[:200] + "…"
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def sanitise(payload: dict) -> dict:
    """Whitelist span attributes, convert micros to millis, drop events/links."""
    clean = _redact(payload)
    if isinstance(clean, dict) and "spans" in clean and isinstance(clean["spans"], list):
        clean["spans"] = [{k: v for k, v in s.items() if k in ATTRIBUTE_ALLOWED} for s in clean["spans"]]
    return clean


def fit(payload, budget: int = MAX_EVIDENCE_BYTES) -> tuple[dict, bool]:
    """Hard byte budget: shrink lists, then drop optional keys, then fail closed."""
    def size(obj) -> int:
        return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    if size(payload) <= budget:
        return {"data": payload, "truncated": False, "bytes": size(payload)}, False
    data = json.loads(json.dumps(payload, ensure_ascii=False))
    truncated = True
    for key in ("spans", "segments", "errors", "traces", "sources"):
        if isinstance(data.get(key), list) and data[key]:
            original = len(data[key])
            while data[key] and size(data) > budget:
                data[key] = data[key][: max(0, len(data[key]) // 2)]
            data[f"{key}_omitted"] = original - len(data[key])
    optional = ["error_spans", "critical_path", "span_details", "span_names_seen"]
    while size(data) > budget and optional:
        data.pop(optional.pop(0), None)
    if size(data) > budget:
        data = {"truncated_over_budget": True, "keys": sorted(data)[:20]}
    return {"data": data, "truncated": truncated, "bytes": size(data)}, truncated
