"""Prove the previous article's read-only service still answers business requests.

This touches only the earlier lab's read-only endpoints (8091 orchestrator + 8089
document search); it never writes and never restarts those services.
"""
import json
import sys
from pathlib import Path

import httpx

A04 = Path("/root/rag-lab/article04-repair-r3")


def main() -> int:
    creds = A04 / "private" / "credentials-for-delivery.json"
    tokens = json.loads(creds.read_text())
    token = tokens.get("lab-a") or next(iter(tokens.values()))
    headers = {"Authorization": "Bearer " + token}
    with httpx.Client(timeout=180) as client:
        health = client.get("http://127.0.0.1:8091/api/health")
        ask = client.post("http://127.0.0.1:8091/api/ask", headers=headers,
                          json={"question": "我的笔记本是什么型号？", "as_of": "2026-03-01"})
        doc = client.post("http://127.0.0.1:8089/api/ask",
                          json={"question": "上海出差住宿每晚最多报多少？",
                                "scope": "all", "as_of": "2026-03-01"})
    result = {
        "orchestrator_health": {"status_code": health.status_code, "body": health.text[:400]},
        "orchestrator_ask": {"status_code": ask.status_code,
                             "body": ask.text[:2000]},
        "document_search": {"status_code": doc.status_code,
                            "body": doc.text[:1500]},
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    ok = (health.status_code == 200 and ask.status_code == 200 and doc.status_code == 200)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
