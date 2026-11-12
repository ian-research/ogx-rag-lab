"""Structured draft: what the user is about to submit, and the hash it is bound to."""
import hashlib
import json
import uuid

SUBMISSION_MEANING = "提交后创建一张待审核申请，尚未批准换新"
NOT_RETRIEVED = "本次未检索到可引用条款"


def _canonical(user_id, version, draft_id, asset, reason, facts, basis, missing):
    """The single authoritative snapshot.

    It carries the user, the draft identity, the version, the asset, the reason,
    the full missing list and the source snapshot, so the back office can derive
    every stored field from it instead of trusting separate arguments.
    """
    return {"user_id": user_id, "version": version, "draft_id": draft_id,
            "asset": asset, "reason": reason, "facts": facts, "basis": basis,
            "missing": missing}


def content_hash(payload) -> str:
    return hashlib.sha256(canonical_text(payload).encode()).hexdigest()


def canonical_text(payload) -> str:  # noqa: F811 (defined before content_hash below)
    """Stable serialisation used both for hashing and for the back-office check."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canonical_from_row(row) -> dict:
    return _canonical(row["user_id"], row["version"], row["draft_id"],
                      json.loads(row["asset_json"] if "asset_json" in row.keys()
                                 else _asset_json(row)),
                      row["reason"],
                      json.loads(row["facts_json"]),
                      json.loads(row["basis_json"]),
                      json.loads(row["missing_json"]))


def _asset_json(row):
    return json.dumps({"asset_id": row["asset_id"], "model": row["asset_model"],
                       "purchase_date": row["purchase_date"],
                       "assigned_date": None, "assigned_date_status": None},
                      ensure_ascii=False)


def asset_view(device):
    return {"asset_id": device.get("asset_id"), "model": device.get("model"),
            "purchase_date": device.get("purchase_date"),
            "assigned_date": device.get("assigned_date"),
            "assigned_date_status": device.get("assigned_date_status")}


def facts_from(device):
    return [
        {"label": "资产编号", "value": device.get("asset_id"), "source": "设备台账"},
        {"label": "型号", "value": device.get("model"), "source": "设备台账"},
        {"label": "购买日期", "value": device.get("purchase_date"), "source": "设备台账"},
        {"label": "领用日期", "value": "台账未提供", "source": "设备台账"},
        {"label": "按购买日计算", "value": f"{device.get('purchase_age')} 年（仅供参考，不等于实际使用时长）",
         "source": "设备台账"},
    ]


CONDITION_TERMS = ("满四年", "维修成本", "重置价", "重置价格", "更换", "换新", "条件",
                   "维修费用", "使用满")


def _score(text):
    return sum(1 for term in CONDITION_TERMS if term in (text or ""))


def _source_view(item, version=None):
    excerpt = (item.get("text") or "")[:400]
    version = version or {}
    return {"chunk_id": item.get("chunk_id"), "heading": item.get("heading"),
            "filename": item.get("filename") or version.get("filename"),
            "policy_id": item.get("policy_id") or version.get("policy_id"),
            "version_label": item.get("version_label") or version.get("version_label"),
            "valid_from": item.get("valid_from") or version.get("valid_from"),
            "valid_to": item.get("valid_to", version.get("valid_to")),
            "excerpt": excerpt,
            "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
            "score": item.get("score")}


def basis_from(docs):
    """Document basis is only what was actually retrieved.

    Sources come back in retrieval order, and the first one is not necessarily the
    clause that answers "what are the conditions". The draft keeps every retrieved
    source with its identifiers, then picks the best matching one by its text — and
    says so when none of them talks about conditions.
    """
    raw = (docs or {}).get("sources") or []
    version = (docs or {}).get("version") or {}
    sources = [_source_view(item, version) for item in raw][:5]
    empty = {"status": "not_retrieved", "policy_id": None, "filename": None,
             "version_label": None, "valid_from": None, "valid_to": None,
             "valid_to_text": None, "citation": NOT_RETRIEVED, "condition": None,
             "excerpt": None, "excerpt_sha256": None, "heading": None, "sources": []}
    if not sources:
        return empty
    best = max(sources, key=lambda item: _score(item["excerpt"]))
    if _score(best["excerpt"]) == 0:
        result = dict(empty)
        result.update({"status": "retrieved_unrelated", "sources": sources,
                       "citation": "本次检索到的条款未直接给出换新条件（见来源）",
                       "filename": sources[0]["filename"], "heading": sources[0]["heading"]})
        return result
    valid_to = best["valid_to"]
    return {
        "status": "retrieved",
        "policy_id": best["policy_id"],
        "filename": best["filename"],
        "version_label": best["version_label"],
        "valid_from": best["valid_from"],
        "valid_to": valid_to,
        "valid_to_text": "未设置结束日期" if not valid_to else valid_to,
        "citation": "《{file}》{ver}｜生效 {frm}｜失效 {to}".format(
            file=best["filename"] or "未命名文档", ver=best["version_label"] or "未标注版本",
            frm=best["valid_from"] or "未记录",
            to="未设置结束日期" if not valid_to else valid_to),
        "condition": best["excerpt"],
        "excerpt": best["excerpt"],
        "excerpt_sha256": best["excerpt_sha256"],
        "heading": best["heading"],
        "chunk_id": best["chunk_id"],
        "sources": sources,
    }


def missing_items(device, docs):
    items = []
    if not device.get("assigned_date"):
        items.append({"item": "实际使用年限", "status": "未知",
                      "why": "台账未提供领用日期，不能把购买日当使用时长"})
    items.append({"item": "维修成本", "status": "台账未提供",
                  "why": "没有维修记录，无法判断是否超过重置价一半"})
    if not (docs or {}).get("sources"):
        items.append({"item": "适用文档依据", "status": "未检索到",
                      "why": "本次查询没有取回可引用的条款片段"})
    return items


def build(user_id, version, device, docs, reason, reason_source="template"):
    asset = asset_view(device)
    facts = facts_from(device)
    basis = basis_from(docs)
    missing = missing_items(device, docs)
    draft_id = uuid.uuid4().hex
    canonical = _canonical(user_id, version, draft_id, asset, reason, facts, basis, missing)
    return {
        "draft_id": draft_id,
        "reason_source": reason_source,
        "user_id": user_id,
        "version": version,
        "asset_id": device.get("asset_id"),
        "asset_model": device.get("model"),
        "purchase_date": device.get("purchase_date"),
        "reason": reason,
        "facts": facts,
        "basis": basis,
        "missing": missing,
        "asset": asset,
        "meaning": SUBMISSION_MEANING,
        "content_hash": content_hash(canonical),
        "canonical_json": canonical_text(canonical),
        "status": "awaiting_confirmation",
    }


def rehash(draft_row, reason):
    """Return (canonical_text, hash) for the edited draft.

    The canonical text is stored with the draft: the bytes that were hashed must be
    the bytes the back office re-hashes, otherwise a legitimate submit fails.
    """
    asset = json.loads(draft_row["asset_json"]) if "asset_json" in draft_row.keys() \
        else json.loads(_asset_json(draft_row))
    payload = _canonical(draft_row["user_id"], draft_row["version"] + 1, draft_row["draft_id"],
                         asset, reason,
                         json.loads(draft_row["facts_json"]), json.loads(draft_row["basis_json"]),
                         json.loads(draft_row["missing_json"]))
    text = canonical_text(payload)
    return text, hashlib.sha256(text.encode()).hexdigest()


def public_view(row):
    return {
        "draft_id": row["draft_id"], "version": row["version"], "status": row["status"],
        "content_hash": row["content_hash"], "created_at": row["created_at"],
        "expires_at": row["expires_at"], "reason": row["reason"],
        "reason_source": (row["reason_source"] if "reason_source" in row.keys() else "template")
        or "template",
        "asset": {"asset_id": row["asset_id"], "model": row["asset_model"],
                  "purchase_date": row["purchase_date"]},
        "facts": json.loads(row["facts_json"]),
        "basis": json.loads(row["basis_json"]),
        "missing": json.loads(row["missing_json"]),
        "meaning": SUBMISSION_MEANING,
    }
