"""Document lookup against the existing version service (8089), same service article 4 used."""
import httpx

import settings


ALLOWED_POLICIES = {"eval3-device", "eval4-inject"}


def search(question: str, policy_id: str = None, as_of: str = None, scope: str = "all"):
    if policy_id not in ALLOWED_POLICIES:
        policy_id = settings.DEFAULT_POLICY
    body = {"question": question, "scope": scope,
            "policy_id": policy_id,
            "as_of": as_of or settings.DEFAULT_AS_OF}
    with httpx.Client(timeout=settings.TOOL_TIMEOUT) as client:
        response = client.post(settings.DOC_SERVICE + "/api/search", json=body)
    if response.status_code != 200:
        return {"status": "unavailable", "http_status": response.status_code}
    data = response.json()
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
