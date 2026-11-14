"""No-model isolation check for the article-6 lab (no model calls).

Creates two ordinary drafts for the two demo identities through the same store
functions the application uses, then verifies over HTTP that each identity can
read its own draft and gets 404 for the other's. The positive control is checked
first, so a 404 cannot be explained by an expired or missing draft.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import draft as draft_module  # noqa: E402
import devices  # noqa: E402
import store  # noqa: E402

BASE = os.getenv("ACTION_BASE", "http://127.0.0.1:8094")
OUT = pathlib.Path(os.getenv("CHECK_OUT", str(ROOT / "evidence" / "live-checks")))
IDENTITIES = {
    "a": ("alice.johnson@company.com", "check-a"),
    "b": ("john.doe@company.com", "check-b"),
}


def read_tokens(path: pathlib.Path) -> dict[str, str]:
    data = json.loads(path.read_text())
    return {"a": data["lab-a"], "b": data["lab-b"]}


def make_draft(email: str, reason: str) -> str:
    device = devices.lookup(email)
    built = draft_module.build(email, 1, device, [], reason, reason_source="user")
    store.init()
    store.create_draft(built)
    return built["draft_id"]


def get(path: str, token: str) -> int:
    with httpx.Client(base_url=BASE, timeout=20) as client:
        return client.get(path, headers={"Authorization": f"Bearer {token}"}).status_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", required=True, help="credentials-for-delivery.json")
    args = parser.parse_args()
    tokens = read_tokens(pathlib.Path(args.credentials))

    ids = {name: make_draft(IDENTITIES[name][0], f"隔离检查草稿 {IDENTITIES[name][1]}") for name in IDENTITIES}
    result = {
        "kind": "isolation-check",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "draft_ids": ids,
        "http": {},
        "note": "No model calls; drafts are created through the same store functions.",
    }
    own = {name: get(f"/api/drafts/{ids[name]}", tokens[name]) for name in ids}
    cross = {"a_reads_b": get(f"/api/drafts/{ids['b']}", tokens["a"]),
             "b_reads_a": get(f"/api/drafts/{ids['a']}", tokens["b"])}
    result["http"]["own"] = own
    result["http"]["cross"] = cross
    result["positive_control"] = all(code == 200 for code in own.values())
    result["isolated"] = all(code == 404 for code in cross.values())
    result["pass"] = result["positive_control"] and result["isolated"]

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"isolation-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"pass": result["pass"], "own": own, "cross": cross, "file": str(target)}))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
