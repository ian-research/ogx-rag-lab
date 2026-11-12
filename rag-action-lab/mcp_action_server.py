"""Simulated back office: one controlled write entry plus two read-back tools.

The write entry verifies the trusted caller and the per-submission proof, then
inserts one ticket under a unique idempotency key inside a transaction.
"""
import asyncio
import hashlib
import json
import json
from datetime import datetime, timezone

from mcp.server.fastmcp import Context, FastMCP
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

import security
import settings
import store

store.init()

mcp = FastMCP("article05-action-mcp", host="127.0.0.1", port=settings.MCP_PORT,
              stateless_http=True, json_response=True)


def trusted(ctx):
    request = getattr(ctx.request_context, "request", None)
    headers = getattr(request, "headers", {})
    if not security.caller_authorized(headers.get("authorization")):
        raise ValueError("调用方未认证")
    identity = (headers.get("x-action-user") or "").strip()
    if not identity or len(identity) > 200:
        raise ValueError("缺少可信身份")
    return identity


def emit(payload):
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
async def create_refresh_ticket(idempotency_key: str, content_hash: str, content_canonical: str,
                          draft_ref: str, asset_id: str, reason: str, proof: str,
                          declared_missing: list[str] | None = None,
                          reason_text: str | None = None,
                          doc_snapshot: dict | None = None,
                          ctx: Context = None) -> str:
    """创建一张待审核的换新申请（模拟）。同一幂等标识只对应一张单。

    The back office does not trust the separate arguments: it parses the canonical
    snapshot, checks that it is the exact byte sequence that was hashed, checks the
    caller identity and draft inside it, and then derives every stored field from
    that snapshot. A payload whose arguments disagree with its own snapshot is
    refused instead of being stored half-and-half.
    """
    identity = trusted(ctx)
    if not (16 <= len(idempotency_key) <= 128 and 64 <= len(content_hash) <= 64):
        return emit({"status": "rejected", "reason": "submit_identifier_invalid"})
    if not security.verify_proof(idempotency_key, content_hash, proof):
        return emit({"status": "rejected", "reason": "proof_invalid"})
    if not content_canonical or len(content_canonical) > 8000:
        return emit({"status": "rejected", "reason": "content_missing"})
    if hashlib.sha256(content_canonical.encode()).hexdigest() != content_hash:
        return emit({"status": "rejected", "reason": "content_hash_mismatch"})
    try:
        canonical = json.loads(content_canonical)
    except ValueError:
        return emit({"status": "rejected", "reason": "content_not_json"})
    if not isinstance(canonical, dict):
        return emit({"status": "rejected", "reason": "content_not_object"})
    # Strict form: re-serialising the parsed object must give back the same bytes, so a
    # caller cannot smuggle a second interpretation of the same hash.
    if json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                  separators=(",", ":")) != content_canonical:
        return emit({"status": "rejected", "reason": "content_not_canonical"})

    canonical_user = canonical.get("user_id")
    canonical_draft = canonical.get("draft_id")
    if canonical_user != identity:
        return emit({"status": "rejected", "reason": "content_user_mismatch"})
    if not draft_ref or len(draft_ref) > 80 or canonical_draft != draft_ref:
        return emit({"status": "rejected", "reason": "content_draft_mismatch"})

    asset = canonical.get("asset") or {}
    canonical_asset = asset.get("asset_id")
    canonical_reason = (canonical.get("reason") or "").strip()
    canonical_missing = [str(item.get("item"))[:120]
                         for item in (canonical.get("missing") or [])][:8]
    basis = canonical.get("basis") or {}
    canonical_snapshot = {k: basis.get(k) for k in
                          ("status", "policy_id", "filename", "version_label",
                           "valid_from", "valid_to", "heading", "excerpt_sha256")}

    if not asset_id or len(asset_id) > 80 or asset_id != canonical_asset:
        return emit({"status": "rejected", "reason": "content_asset_mismatch"})
    if not canonical_reason or len(canonical_reason) > 800:
        return emit({"status": "rejected", "reason": "reason_invalid"})
    if reason.strip() != canonical_reason:
        return emit({"status": "rejected", "reason": "content_reason_mismatch"})
    if (reason_text or "").strip() and (reason_text or "").strip() != canonical_reason:
        return emit({"status": "rejected", "reason": "content_reason_mismatch"})
    if [str(item)[:120] for item in (declared_missing or [])][:8] != canonical_missing:
        return emit({"status": "rejected", "reason": "content_missing_mismatch"})
    given_snapshot = {k: (doc_snapshot or {}).get(k) for k in canonical_snapshot}
    if given_snapshot != canonical_snapshot:
        return emit({"status": "rejected", "reason": "content_source_mismatch"})

    payload = {"draft_ref": canonical_draft, "submitted_by": identity,
               "asset_id": canonical_asset,
               "reason_text": canonical_reason,
               "declared_missing": canonical_missing,
               "doc_snapshot": canonical_snapshot,
               "version": canonical.get("version"),
               "content_hash": content_hash, "content_canonical": content_canonical,
               "ticket_status": "pending_review",
               "meaning": "待审核申请，不代表已批准换新"}
    outcome, row = store.insert_ticket(idempotency_key, identity, canonical_asset,
                                       canonical_reason, content_hash, payload)
    if outcome == "conflict":
        store.audit(identity, "submit_conflict", {"idempotency_key": idempotency_key})
        return emit({"status": "conflict", "reason": "same_key_different_content",
                     "ticket_no": row["ticket_no"]})
    store.audit(identity, "ticket_" + outcome,
                {"ticket_no": row["ticket_no"], "idempotency_key": idempotency_key})
    if settings.FAULT_FILE.exists():
        # Fault injection: the write is committed, the response is deliberately lost.
        settings.FAULT_FILE.unlink()
        # Yield instead of blocking: the ticket is already committed and must stay
        # queryable while this response is delayed.
        await asyncio.sleep(settings.FAULT_DELAY)
    return emit({"status": "submitted", "ticket_no": row["ticket_no"],
                 "ticket_status": row["status"], "duplicate": outcome == "duplicate",
                 "created_at": row["created_at"]})


@mcp.tool()
def lookup_refresh_ticket(idempotency_key: str, ctx: Context = None) -> str:
    """按提交标识回查受理结果；用于写入结果未知时的核对。"""
    identity = trusted(ctx)
    row = store.ticket_by_key(idempotency_key)
    if row is None or row["user_id"] != identity:
        return emit({"found": False})
    payload = json.loads(row["payload_json"])
    return emit({"found": True, "ticket_no": row["ticket_no"], "status": row["status"],
                 "content_hash": row["content_hash"], "created_at": row["created_at"],
                 "asset_id": row["asset_id"],
                 "draft_ref": payload.get("draft_ref"),
                 "version": payload.get("version"),
                 "declared_missing": payload.get("declared_missing", []),
                 "reason_text": payload.get("reason_text"),
                 "doc_snapshot": payload.get("doc_snapshot")})


@mcp.tool()
def get_refresh_ticket(ticket_no: str, ctx: Context = None) -> str:
    """按工单号查询本人申请；他人与不存在返回同一结果。"""
    identity = trusted(ctx)
    row = store.get_ticket(ticket_no)
    if row is None or row["user_id"] != identity:
        return emit({"status": "no_record"})
    return emit({"status": "ok", "ticket_no": row["ticket_no"], "ticket_status": row["status"],
                 "asset_id": row["asset_id"], "created_at": row["created_at"],
                 "payload": json.loads(row["payload_json"])})


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    try:
        with store.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "ok", "role": "simulated-back-office"})
    except Exception as error:
        return JSONResponse({"status": "degraded", "error": str(error)[:120]}, status_code=503)


class TrustedCaller:
    """Protect initialize/list/call endpoints, not just individual tool bodies."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            headers = Headers(scope=scope)
            try:
                allowed = security.caller_authorized(headers.get("authorization"))
            except (OSError, ValueError, RuntimeError):
                allowed = False
            if not allowed:
                await JSONResponse({"status": "unauthenticated"}, status_code=401)(scope, receive, send)
                return
        await self.inner(scope, receive, send)


app = TrustedCaller(mcp.streamable_http_app())
