"""Compare what the user previewed, confirmed, sent and what the back office stored.

Usage:  .venv/bin/python scripts/fieldcheck.py [idem_key]

Four snapshots are compared field by field, including the document version window
and the excerpt digest — not just the hash string:

  1. draft (what the page showed)
  2. confirmation (what the user confirmed)
  3. submission request (what the application was authorized to send)
  4. ticket payload (what the back office actually accepted)

The request's canonical text is re-parsed and re-hashed, so the check covers the
bytes as well as the fields.
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store  # noqa: E402

DOC_FIELDS = ("status", "policy_id", "filename", "version_label", "valid_from",
              "valid_to", "heading", "excerpt_sha256")


def load(json_text):
    return json.loads(json_text) if json_text else {}


def main() -> int:
    with store.connect() as conn:
        if len(sys.argv) > 1:
            row = conn.execute("SELECT * FROM submissions WHERE idem_key=?",
                               (sys.argv[1],)).fetchone()
        else:
            row = conn.execute("SELECT * FROM submissions ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:
            print("no submission found")
            return 1
        draft = conn.execute("SELECT * FROM drafts WHERE draft_id=?", (row["draft_id"],)).fetchone()
        confirmation = conn.execute("SELECT * FROM confirmations WHERE confirmation_id=?",
                                    (row["confirmation_id"],)).fetchone()
        ticket = conn.execute("SELECT * FROM tickets WHERE idem_key=?", (row["idem_key"],)).fetchone()

    basis = load(draft["basis_json"])
    missing = load(draft["missing_json"])
    facts = load(draft["facts_json"])
    request = load(row["request_json"])
    payload = load(ticket["payload_json"]) if ticket else {}
    canonical = load(request.get("content_canonical"))
    sent_doc = request.get("doc_snapshot") or {}
    stored_doc = payload.get("doc_snapshot") or {}

    checks = []

    def check(name, left, right):
        checks.append({"field": name, "ok": left == right, "preview": left, "accepted": right})

    check("用户", canonical.get("user_id"), payload.get("submitted_by"))
    check("草稿身份", canonical.get("draft_id"), draft["draft_id"])
    check("草稿身份（受理）", canonical.get("draft_id"), payload.get("draft_ref"))
    check("版本", draft["version"], canonical.get("version"))
    check("版本（受理）", draft["version"], payload.get("version"))
    check("资产编号", draft["asset_id"], (canonical.get("asset") or {}).get("asset_id"))
    check("资产编号（受理）", draft["asset_id"], payload.get("asset_id") or (ticket["asset_id"] if ticket else None))
    check("型号", draft["asset_model"], (canonical.get("asset") or {}).get("model"))
    check("理由", draft["reason"], canonical.get("reason"))
    check("理由（发送）", draft["reason"], request.get("reason_text"))
    check("理由（受理）", draft["reason"], payload.get("reason_text"))
    check("事实快照", facts, canonical.get("facts"))
    check("缺项（预览）", missing, canonical.get("missing"))
    check("缺项（发送）", [item.get("item") for item in missing], request.get("declared_missing"))
    check("缺项（受理）", [item.get("item") for item in missing], payload.get("declared_missing"))

    for field in DOC_FIELDS:
        check(f"文档 {field}", (canonical.get("basis") or {}).get(field), sent_doc.get(field))
        check(f"文档 {field}（受理）", (canonical.get("basis") or {}).get(field), stored_doc.get(field))
    for field in ("status", "filename", "valid_from", "valid_to", "heading", "excerpt_sha256"):
        check(f"预览-规范 {field}", basis.get(field), (canonical.get("basis") or {}).get(field))

    check("内容哈希（确认）", draft["content_hash"], confirmation["content_hash"] if confirmation else None)
    check("确认版本", draft["version"], confirmation["version"] if confirmation else None)
    check("确认草稿", draft["draft_id"], confirmation["draft_id"] if confirmation else None)
    check("内容哈希（发送）", draft["content_hash"], request.get("content_hash"))
    check("内容哈希（受理）", draft["content_hash"], payload.get("content_hash"))
    canonical_text = request.get("content_canonical") or ""
    check("哈希可重算", hashlib.sha256(canonical_text.encode()).hexdigest(),
          request.get("content_hash"))
    check("规范文本可重现",
          json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
          canonical_text)
    sources = basis.get("sources") or []
    if basis.get("status") == "not_retrieved":
        sources_ok = not sources
    else:
        sources_ok = bool(sources) and all(item.get("chunk_id") for item in sources)
    check("来源片段保留", sources_ok, True)

    width = max(len(c["field"]) for c in checks) + 2
    for item in checks:
        mark = "OK  " if item["ok"] else "DIFF"
        print(f"{mark} {item['field']:<{width}} {item['preview']!r} -> {item['accepted']!r}")
    failed = [c["field"] for c in checks if not c["ok"]]
    print(f"\nidem={row['idem_key'][:12]}… draft={draft['draft_id']} "
          f"ticket={(ticket['ticket_no'] if ticket else None)} checks={len(checks)}")
    print("fieldcheck:", "PASS" if not failed else f"FAIL ({', '.join(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
