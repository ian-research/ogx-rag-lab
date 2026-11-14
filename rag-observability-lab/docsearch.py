"""Document lookup against the existing version service (8089), same service article 4 used."""
import time

import httpx

import metrics
import settings
import telemetry


ALLOWED_POLICIES = {"eval3-device", "eval4-inject"}


def search(question: str, policy_id: str = None, as_of: str = None, scope: str = "all"):
    if policy_id not in ALLOWED_POLICIES:
        policy_id = settings.DEFAULT_POLICY
    body = {"question": question, "scope": scope,
            "policy_id": policy_id,
            "as_of": as_of or settings.DEFAULT_AS_OF}
    started = time.monotonic()
    injected = settings.DOC_FAULT_FILE.exists()
    with telemetry.span("doc.search", **{"lab.policy_id": policy_id,
                                         "lab.as_of": body["as_of"],
                                         "peer.service": "version-service"}) as current:
        if injected:
            # Test-only switch: proves the application branch without touching 8089.
            telemetry.set_attrs(current, **{"lab.injected_fault": True,
                                            "lab.outcome": "injected_unavailable"})
            telemetry.mark_error(current, "doc_injected_unavailable")
            metrics.observe_tool("doc.search", "injected", 0.0)
            return {"status": "unavailable", "http_status": 503, "injected": True}
        try:
            with httpx.Client(timeout=settings.TOOL_TIMEOUT) as client:
                response = client.post(settings.DOC_SERVICE + "/api/search", json=body)
        except httpx.HTTPError as error:
            elapsed = time.monotonic() - started
            metrics.observe_tool("doc.search", "unreachable", elapsed)
            telemetry.set_attrs(current, **{"lab.outcome": "unreachable"})
            telemetry.mark_error(current, "doc_unreachable", type(error).__name__)
            return {"status": "unavailable", "http_error": type(error).__name__}
        elapsed = time.monotonic() - started
        if response.status_code != 200:
            metrics.observe_tool("doc.search", "http_" + str(response.status_code), elapsed)
            telemetry.set_attrs(current, **{"lab.outcome": "http_error",
                                            "http.response.status_code": response.status_code})
            telemetry.mark_error(current, "doc_http_" + str(response.status_code))
            return {"status": "unavailable", "http_status": response.status_code}
        try:
            data = response.json()
        except ValueError:
            metrics.observe_tool("doc.search", "invalid_json", elapsed)
            telemetry.set_attrs(current, **{"lab.outcome": "invalid_json"})
            telemetry.mark_error(current, "doc_invalid_json")
            return {"status": "unavailable", "http_status": 200, "parse": "invalid_json"}
        metrics.observe_tool("doc.search", "ok", elapsed)
        telemetry.set_attrs(current, **{"lab.outcome": "ok",
                                        "lab.source_count": len(data.get("sources") or [])})
    sources = []
    for item in data.get("sources", [])[:3]:
        sources.append({
            "filename": item.get("filename"),
            "policy_id": item.get("policy_id"),
            "version_label": item.get("version_label"),
            "valid_from": item.get("valid_from"),
            "valid_to": item.get("valid_to"),
            "heading": item.get("heading"),
            "text": (item.get("text") or "")[:600],
            "chunk_id": item.get("chunk_id"),
        })
    return {"status": data.get("status"), "as_of": data.get("as_of"),
            "version": data.get("version"), "sources": sources}
