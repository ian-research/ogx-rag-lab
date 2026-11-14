"""No-model check: a confirmation stops working after the draft is revised.

Creates one draft through the same store functions the application uses, confirms
it over HTTP, revises it, then submits with the old capability. The expected
outcome is an exact 409 and no ticket. Only booleans, HTTP codes and versions are
recorded; the capability value itself is never written anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import draft as draft_module  # noqa: E402
import devices  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402

BASE = os.getenv("ACTION_BASE", "http://127.0.0.1:8094")
OUT = pathlib.Path(os.getenv("CHECK_OUT", str(ROOT / "evidence" / "live-checks")))
EMAIL = "alice.johnson@company.com"


def tickets_for(draft_id: str) -> int:
    conn = sqlite3.connect(f"file:{settings.DB_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE payload_json LIKE ?", (f"%{draft_id}%",)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", required=True)
    args = parser.parse_args()
    token = json.loads(pathlib.Path(args.credentials).read_text())["lab-a"]

    built = draft_module.build(EMAIL, 1, devices.lookup(EMAIL), [], "确认失效检查", reason_source="user")
    store.init()
    store.create_draft(built)
    draft_id = built["draft_id"]
    headers = {"Authorization": f"Bearer {token}"}

    result = {
        "kind": "confirmation-retirement-check",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "draft_id": draft_id,
        "note": "No model calls; confirms over HTTP, then revises, then reuses the old capability.",
    }
    with httpx.Client(base_url=BASE, timeout=30, headers=headers) as client:
        confirm = client.post(f"/api/drafts/{draft_id}/confirm",
                              json={"version": 1, "content_hash": built["content_hash"]})
        confirmed = confirm.json() if confirm.status_code == 200 else {}
        capability = confirmed.get("confirmation_id")
        revise = client.post(f"/api/drafts/{draft_id}/revise", json={"reason": "改稿后的理由"})
        revised = revise.json().get("draft", {}) if revise.status_code == 200 else {}
        tickets_before = tickets_for(draft_id)
        if capability:
            submit = client.post(f"/api/drafts/{draft_id}/submit",
                                 json={"confirmation_id": capability})
            submit_status = submit.status_code
        else:
            submit_status = None
        tickets_after = tickets_for(draft_id)

    result["http"] = {"confirm": confirm.status_code, "revise": revise.status_code,
                      "stale_submit": submit_status}
    result["versions"] = {"before": 1, "after_revise": revised.get("version")}
    result["checks"] = {
        "capability_present": bool(capability),
        "revise_succeeded": revise.status_code == 200 and (revised.get("version") or 0) > 1,
        "stale_submit_is_409": submit_status == 409,
        "no_ticket_created": tickets_before == 0 and tickets_after == 0,
    }
    result["pass"] = all(result["checks"].values())

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"confirmation-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"pass": result["pass"], "checks": result["checks"],
                      "http": result["http"], "file": str(target)}))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
