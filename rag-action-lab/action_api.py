"""Application side of the action lab: draft, page confirmation, controlled submission.

Not OGX native MCP orchestration. The model loop can only read; submission is
triggered by an explicit page action and executed by this process.
"""
import asyncio
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone

import httpx
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, Field

import draft as draft_mod
import model_loop
import security
import settings
import store

@asynccontextmanager
async def lifespan(_app):
    store.init()
    yield


app = FastAPI(title="Action lab", docs_url=None, redoc_url=None, lifespan=lifespan)

SUBMIT_KEYWORDS = ("申请", "提交", "办理")


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=800)
    as_of: str = Field(default=settings.DEFAULT_AS_OF, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ReviseBody(BaseModel):
    reason: str = Field(min_length=4, max_length=800)


class SubmitBody(BaseModel):
    # A submission must name the confirmation the page just issued; the server checks
    # that record inside the same transaction that freezes the draft.
    confirmation_id: str = Field(min_length=8, max_length=80)


class ConfirmBody(BaseModel):
    version: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)


def identity_of(authorization):
    session = security.session_identity(authorization)
    if session is None:
        raise HTTPException(401, "会话无效，请重新连接")
    return session


def headers_for(identity):
    return {"Authorization": "Bearer " + security.service_key(), "X-Action-User": identity}


async def mcp_tool(name, arguments, identity):
    async with streamablehttp_client(settings.ACTION_MCP_URL, headers=headers_for(identity),
                                     sse_read_timeout=settings.SUBMIT_TIMEOUT) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await asyncio.wait_for(session.call_tool(name, arguments),
                                            timeout=settings.SUBMIT_TIMEOUT)
    if result.isError:
        raise RuntimeError("写入口返回执行错误")
    blocks = getattr(result, "content", [])
    if len(blocks) != 1 or getattr(blocks[0], "type", None) != "text":
        raise RuntimeError("写入口返回格式不符合约定")
    return json.loads(blocks[0].text)


def record_run(kind, payload):
    settings.RUN_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{uuid.uuid4().hex[:12]}"
    path = settings.RUN_ROOT / f"{run_id}.json"
    security.write_private(path, json.dumps({"kind": kind, "run_id": run_id, "at": store.utcnow(), **payload},
                                            ensure_ascii=False, indent=1))
    return run_id


@app.get("/api/health")
def health():
    try:
        with store.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        db = "ok"
    except Exception as error:
        return JSONResponse({"status": "degraded", "db": str(error)[:120]}, status_code=503)
    return {"status": "ok", "db": db, "service": "article05-action-lab"}


@app.post("/api/connect")
def connect(authorization: str | None = Header(default=None)):
    session = identity_of(authorization)
    return {"status": "ok", "label": session["label"], "session_id": session["session_id"]}


@app.post("/api/ask")
def ask(body: AskBody, authorization: str | None = Header(default=None)):
    session = identity_of(authorization)
    identity = session["identity"]
    started = time.monotonic()
    try:
        result = model_loop.run(body.question, identity, body.as_of)
    except model_loop.LoopError as error:
        record_run("ask", {"identity": session["label"], "question": body.question,
                           "error": str(error)})
        raise HTTPException(502, f"查询未完成：{error}")
    wants_draft = any(word in body.question for word in SUBMIT_KEYWORDS) and ("换" in body.question)
    payload = {"status": "ok", "answer": result["answer"], "steps": result["steps"],
               "as_of": body.as_of, "ms": int((time.monotonic() - started) * 1000),
               "kind": "answer", "as_of_display": body.as_of}
    if wants_draft:
        device = result.get("device") or {}
        if device.get("status") != "ok":
            # Keep what the model actually said; never summarise it as "no device record",
            # which is only true when the tool said so.
            payload["kind"] = "answer"
            payload["draft_note"] = ("本轮没有生成草稿：需要先查到本人设备台账。" if device.get("status") == "no_record"
                                     else "本轮没有生成草稿。")
            payload["run_id"] = record_run("ask", {"identity": session["label"], "question": body.question,
                                                   "steps": result["steps"], "draft": None})
            return payload
        # The reason is the user's own statement when the model recorded one; the
        # generic sentence is only a fallback and never replaces a stated reason.
        stated = (result.get("user_reason") or "").strip()
        if stated:
            reason, reason_source = stated[:300], "user"
        else:
            reason = "申请更换笔记本（{model}，{asset}）。使用时长与维修成本按台账实际情况核定。".format(
                model=device.get("model") or "未记录型号", asset=device.get("asset_id") or "未记录编号")
            reason_source = "template"
        built = draft_mod.build(identity, 1, device, result.get("docs") or {}, reason,
                                reason_source=reason_source)
        row = store.create_draft(built)
        store.audit(identity, "draft_created", {"draft_id": row["draft_id"]})
        payload.update({"kind": "draft", "draft": draft_mod.public_view(row)})
        payload["run_id"] = record_run("ask", {"identity": session["label"], "question": body.question,
                                               "steps": result["steps"], "draft_id": row["draft_id"],
                                               "content_hash": row["content_hash"]})
    else:
        payload["run_id"] = record_run("ask", {"identity": session["label"], "question": body.question,
                                               "steps": result["steps"], "draft": None})
    return payload


def owned_draft(draft_id, identity):
    row = store.get_draft(draft_id)
    if row is None or row["user_id"] != identity:
        raise HTTPException(404, "草稿不存在")
    return row


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: str, authorization: str | None = Header(default=None)):
    session = identity_of(authorization)
    row = owned_draft(draft_id, session["identity"])
    return {"status": "ok", "draft": draft_mod.public_view(row)}


@app.post("/api/drafts/{draft_id}/revise")
def revise(draft_id: str, body: ReviseBody, authorization: str | None = Header(default=None)):
    session = identity_of(authorization)
    owned_draft(draft_id, session["identity"])
    row = store.get_draft(draft_id)
    canonical_json, new_hash = draft_mod.rehash(row, body.reason)
    updated, error = store.revise_draft(draft_id, session["identity"], body.reason,
                                        new_hash, canonical_json)
    if error == "not_found":
        raise HTTPException(404, "草稿不存在")
    if error is not None:
        raise HTTPException(409, _frozen_message(error))
    store.audit(session["identity"], "draft_revised",
                {"draft_id": draft_id, "version": updated["version"]})
    return {"status": "ok", "draft": draft_mod.public_view(updated),
            "note": "内容已修改，旧确认失效，请重新预览后确认"}


def _frozen_message(state):
    return {"submitting": "该草稿正在提交，内容已冻结，不能修改或取消",
            "submitted_unknown": "上次提交结果尚未确认，草稿已冻结；请先回查，不要改稿重提",
            "submitted": "该草稿已提交，内容已冻结",
            "cancelled": "该草稿已取消"}.get(state, f"该草稿当前状态为 {state}，不能修改")


@app.post("/api/drafts/{draft_id}/confirm")
def confirm(draft_id: str, body: ConfirmBody, authorization: str | None = Header(default=None)):
    """A confirmation is only valid for the exact version the user is looking at."""
    session = identity_of(authorization)
    row = owned_draft(draft_id, session["identity"])
    if row["status"] != "awaiting_confirmation":
        raise HTTPException(409, _frozen_message(row["status"]))
    if row["expires_at"] <= store.utcnow():
        raise HTTPException(409, "草稿已过期，请重新生成")
    if body.version != row["version"] or body.content_hash != row["content_hash"]:
        raise HTTPException(409, "页面内容已变化，请重新预览后再确认")
    confirmation = store.create_confirmation(row, uuid.uuid4().hex)
    return {"status": "ok", "confirmation_id": confirmation["confirmation_id"],
            "version": row["version"], "content_hash": row["content_hash"],
            "expires_at": confirmation["expires_at"],
            "meaning": draft_mod.SUBMISSION_MEANING}


@app.post("/api/drafts/{draft_id}/cancel")
def cancel(draft_id: str, authorization: str | None = Header(default=None)):
    session = identity_of(authorization)
    row, error = store.cancel_draft(draft_id, session["identity"])
    if error == "not_found":
        raise HTTPException(404, "草稿不存在")
    if error is not None:
        raise HTTPException(409, _frozen_message(error))
    return {"status": "ok", "draft": draft_mod.public_view(row)}


def idempotency_key(user_id, draft_id, version, content_hash):
    return security.sign(f"idem|{user_id}|{draft_id}|{version}|{content_hash}")


@app.post("/api/drafts/{draft_id}/submit")
async def submit(draft_id: str, body: SubmitBody, authorization: str | None = Header(default=None)):
    session = identity_of(authorization)
    identity = session["identity"]
    row = owned_draft(draft_id, identity)
    if row["status"] == "cancelled":
        raise HTTPException(409, "草稿已取消，不能提交")

    # One draft has one stable identifier while it is being submitted. Validation of
    # the confirmation, the freeze and the submission record happen together.
    idem = row["idem_key"] or idempotency_key(identity, draft_id, row["version"],
                                              row["content_hash"])
    locked, submission, error = store.authorize_and_lock(
        draft_id, identity, idem, body.confirmation_id)
    _fault("after-authorize")

    if error == "orphan_lock":
        return await _recover_without_authorization(idem, identity, draft_id, locked, session)
    if error is not None:
        raise HTTPException(404 if error == "not_found" else 409, _submit_error(error))

    if submission["state"] == "submitted":
        return {"status": "submitted", "idem_key": idem, "ticket_no": submission["ticket_no"],
                "ticket_status": "pending_review", "note": "repeat: same submission, no new ticket"}

    if submission["state"] in ("needs_recheck", "submitted_unknown", "manual_review"):
        return await _resume(idem, identity, draft_id, submission, locked, session)
    if submission["state"] == "submitting" and submission["request_json"]:
        # An earlier attempt saved its request: recover it instead of writing a new one.
        return await _resume(idem, identity, draft_id, submission, locked, session)

    arguments = (json.loads(submission["request_json"]) if submission["request_json"]
                 else _arguments_from_row(locked, idem))
    return await _write_and_verify(idem, identity, draft_id, arguments, session)


def _frozen_arguments(submission, locked, idem):
    """The request the user authorized: stored bytes if present, else the frozen row."""
    if submission["request_json"]:
        return json.loads(submission["request_json"])
    return _arguments_from_row(locked, idem)


def _finalize(idem, identity, draft_id, ticket_no, session, note):
    store.update_submission(idem, "submitted", ticket_no=ticket_no)
    store.set_draft_status(draft_id, "submitted", actor=identity)
    with store.connect() as conn:
        conn.execute("UPDATE confirmations SET status='used', used_at=? "
                     "WHERE draft_id=? AND status='active'", (store.utcnow(), draft_id))
    run_id = record_run("submit", {"identity": session["label"], "draft_id": draft_id,
                                   "idem_key": idem, "outcome": "submitted",
                                   "ticket_no": ticket_no})
    return {"status": "submitted", "idem_key": idem, "ticket_no": ticket_no,
            "ticket_status": "pending_review", "run_id": run_id, "message": note}


def _needs_recheck(idem, identity, draft_id, message, error, session):
    store.update_submission(idem, "needs_recheck", error=error)
    store.release_draft(draft_id, "submitted_unknown")
    run_id = record_run("submit", {"identity": session["label"], "draft_id": draft_id,
                                   "idem_key": idem, "outcome": "needs_recheck", "error": error})
    return JSONResponse({"status": "needs_recheck", "idem_key": idem, "run_id": run_id,
                         "message": message}, status_code=202)


def _manual_review(idem, identity, draft_id, reason, session):
    store.update_submission(idem, "manual_review", error=reason[:200])
    store.set_draft_status(draft_id, "submitted_unknown", actor=identity)
    run_id = record_run("submit", {"identity": session["label"], "draft_id": draft_id,
                                   "idem_key": idem, "outcome": "manual_review",
                                   "error": reason})
    raise HTTPException(409, f"{reason}；草稿保持冻结，未解锁，请人工核对后再决定")


async def _resume(idem, identity, draft_id, submission, locked, session):
    """Continue a submission that stopped mid-flight, without changing its content.

    Order matters: ask the back office first (read-only), and only replay the frozen
    request when the answer is decisive "no such record". An unusable answer keeps the
    key and the freeze, because replaying on a guess is how duplicates appear.
    """
    if submission["state"] == "manual_review":
        raise HTTPException(409, "该提交缺少可信的授权记录，需人工核对；草稿保持冻结，未解锁")

    verified = await _lookup(idem, identity)
    if verified.get("found"):
        arguments = _frozen_arguments(submission, locked, idem)
        ok, why = _matches_snapshot(verified, arguments)
        if not ok:
            return _manual_review(idem, identity, draft_id, f"回查记录与授权快照不符：{why}", session)
        return _finalize(idem, identity, draft_id, verified["ticket_no"], session,
                         "按同一标识回查到了已完成的上次提交")
    if not verified.get("decisive"):
        return JSONResponse({"status": "recovering_unknown", "idem_key": idem,
                             "message": "回查暂时失败，无法判断是否已写入；草稿保持冻结，"
                                        "原提交标识不变，请稍后用同一标识回查。"}, status_code=202)

    if not submission["authorization_json"]:
        return _manual_review(idem, identity, draft_id,
                              "历史版本留下的未决提交缺少授权记录，无法安全自动恢复", session)

    owner = secrets.token_hex(8)
    if not store.take_lease(idem, owner):
        return JSONResponse({"status": "submitting", "idem_key": idem,
                             "message": "该提交正在处理中，请按同一标识回查。"}, status_code=202)
    arguments = _frozen_arguments(submission, locked, idem)
    return await _write_and_verify(idem, identity, draft_id, arguments, session, owner=owner)


async def _recover_without_authorization(idem, identity, draft_id, locked, session):
    """A frozen draft with no trustworthy authorization record: never unlock on a guess."""
    verified = await _lookup(idem, identity)
    if verified.get("found"):
        arguments = _arguments_from_row(locked, idem)
        ok, why = _matches_snapshot(verified, arguments)
        if not ok:
            return _manual_review(idem, identity, draft_id, f"回查记录与草稿快照不符：{why}", session)
        return _finalize(idem, identity, draft_id, verified["ticket_no"], session,
                         "按同一标识回查到了已完成的上次提交")
    reason = ("历史提交缺少授权记录，且回查未命中" if verified.get("decisive")
              else "历史提交缺少授权记录，回查暂不可用")
    return _manual_review(idem, identity, draft_id, reason, session)


async def _write_and_verify(idem, identity, draft_id, arguments, session, owner=None):
    """Write with the frozen request, then require a complete matching lookup."""
    store.update_submission(idem, "submitting")
    store.set_submission_request(idem, arguments)
    _fault("after-request")
    try:
        result = await mcp_tool("create_refresh_ticket", arguments, identity)
    except Exception as error:
        return _needs_recheck(idem, identity, draft_id,
                              "提交结果待核对：写入可能已完成，请按同一标识回查，不要改稿重提。",
                              f"写入结果未知：{type(error).__name__}", session)

    _fault("after-write")

    if result.get("status") == "rejected":
        store.update_submission(idem, "failed", error=str(result.get("reason"))[:200])
        store.release_draft(draft_id, "awaiting_confirmation", clear_key=True)
        raise HTTPException(422, f"写入口拒绝：{result.get('reason')}")

    if result.get("status") not in ("submitted", "duplicate") or not result.get("ticket_no"):
        return _needs_recheck(idem, identity, draft_id,
                              "写入口没有返回可验证的工单编号，不能视为已受理。",
                              "写入口未返回可验证的工单编号", session)

    verified = await _lookup(idem, identity)
    ok, why = _matches_snapshot(verified, arguments)
    if not ok:
        if not verified.get("decisive") and not verified.get("found"):
            return _needs_recheck(idem, identity, draft_id,
                                  "回查暂不可用，无法确认受理结果；请稍后按同一标识回查。",
                                  f"回查不可用：{why}", session)
        return _needs_recheck(idem, identity, draft_id,
                              "回查记录与本次授权的快照不一致，不能确认已受理。", why, session)

    return _finalize(idem, identity, draft_id, verified["ticket_no"], session,
                     "已创建待审核申请，尚未批准换新。")


DOC_FIELDS = ("status", "policy_id", "filename", "version_label", "valid_from",
              "valid_to", "heading", "excerpt_sha256")


def _matches_snapshot(verified, arguments):
    """The lookup must return every protected field and it must equal the frozen request.

    Missing fields are not "unknown, so skip": a response that cannot be compared is
    not evidence that the write happened. All callers (submit, reconcile, recovery)
    go through this one function so the contract cannot drift per entry point.
    """
    if not verified.get("found") or not verified.get("ticket_no"):
        return False, "回查未命中"
    if verified.get("content_hash") != arguments.get("content_hash"):
        return False, "回查的 content_hash 与授权快照不一致或缺失"
    if verified.get("asset_id") != arguments.get("asset_id"):
        return False, "回查的资产编号与授权快照不一致或缺失"
    if verified.get("draft_ref") != arguments.get("draft_ref"):
        return False, "回查的草稿号与授权快照不一致或缺失"
    if verified.get("version") != arguments.get("version"):
        return False, "回查的版本与授权快照不一致或缺失"
    if verified.get("reason_text") != arguments.get("reason_text"):
        return False, "回查的理由与授权快照不一致或缺失"
    if list(verified.get("declared_missing") or []) != list(arguments.get("declared_missing") or []):
        return False, "回查的缺项与授权快照不一致或缺失"
    doc = verified.get("doc_snapshot")
    if not isinstance(doc, dict):
        return False, "回查缺少文档快照"
    for field in DOC_FIELDS:
        if field not in doc:
            return False, f"回查的文档快照缺少 {field}"
        if doc[field] != (arguments.get("doc_snapshot") or {}).get(field):
            return False, f"回查的文档快照 {field} 与授权快照不一致"
    return True, ""


def _arguments_from_row(locked, idem):
    """The frozen request for a draft, built from the locked row only."""
    missing = json.loads(locked["missing_json"])
    basis = json.loads(locked["basis_json"])
    return {
        "idempotency_key": idem,
        "content_hash": locked["content_hash"],
        "content_canonical": locked["canonical_json"],
        "draft_ref": locked["draft_id"],
        "asset_id": locked["asset_id"],
        "reason": locked["reason"],
        "reason_text": locked["reason"],
        "declared_missing": [item["item"] for item in missing],
        "doc_snapshot": {"status": basis.get("status"), "policy_id": basis.get("policy_id"),
                         "filename": basis.get("filename"),
                         "version_label": basis.get("version_label"),
                         "valid_from": basis.get("valid_from"), "valid_to": basis.get("valid_to"),
                         "heading": basis.get("heading"),
                         "excerpt_sha256": basis.get("excerpt_sha256")},
        "version": locked["version"],
        "proof": security.proof_for(idem, locked["content_hash"]),
    }


def _fault(stage):
    """Test hook: stop the process at a persistence boundary (never set in production)."""
    if os.environ.get("ACTION_FAULT_STAGE") == stage:
        os._exit(70)


def _submit_error(error):
    return {"confirmation_missing": "提交需要有效的确认记录，请在页面上确认后再提交",
            "confirmation_other_draft": "确认记录属于另一份草稿，不能用于本次提交",
            "confirmation_expired": "确认已过期，请重新确认",
            "confirmation_stale": "确认与当前草稿不一致，请重新预览并确认",
            "confirmation_superseded": "确认已失效（草稿修改过），请重新确认",
            "confirmation_used": "该确认已用于另一次提交",
            "key_changed": "该草稿的提交标识已固定，不能更换内容后重新提交",
            "submitting": "该草稿正在提交，内容已冻结，不能修改或取消",
            "submitted_unknown": "上次提交结果尚未确认，草稿已冻结；请先回查，不要改稿重提",
            "submitted": "该草稿已提交，内容已冻结",
            }.get(error, f"不能提交（{error}）")


async def _lookup(idem, identity):
    """Three outcomes, never two.

    found=True        a record exists (decisive)
    found=False, decisive=True   the back office answered and has no such record
    decisive=False    the call failed or the answer was unusable; we do not know
    """
    try:
        result = await mcp_tool("lookup_refresh_ticket", {"idempotency_key": idem}, identity)
    except Exception as error:
        return {"found": False, "decisive": False, "error": type(error).__name__}
    if not isinstance(result, dict):
        return {"found": False, "decisive": False, "error": "unusable response"}
    if result.get("found"):
        return dict(result, decisive=True)
    if result.get("found") is False:
        return {"found": False, "decisive": True}
    return {"found": False, "decisive": False, "error": "unusable response"}


def _submission_view(submission, note=None):
    return {"status": submission["state"], "idem_key": submission["idem_key"],
            "ticket_no": submission["ticket_no"], "note": note,
            "last_error": submission["last_error"]}


@app.get("/api/submissions")
def submissions(authorization: str | None = Header(default=None)):
    session = identity_of(authorization)
    rows = store.list_submissions(session["identity"])
    return {"status": "ok", "items": [_submission_view(r) for r in rows]}


@app.post("/api/submissions/{idem}/reconcile")
async def reconcile(idem: str, authorization: str | None = Header(default=None)):
    session = identity_of(authorization)
    identity = session["identity"]
    submission = store.get_submission(idem)
    if submission is None or submission["user_id"] != identity:
        raise HTTPException(404, "提交记录不存在")
    if submission["state"] == "submitted":
        return {"status": "submitted", "ticket_no": submission["ticket_no"], "idem_key": idem}

    found = await _lookup(idem, identity)
    if found.get("found"):
        if submission["request_json"]:
            ok, why = _matches_snapshot(found, json.loads(submission["request_json"]))
        else:
            ok, why = False, "缺少原始请求快照，无法比对"
        if not ok:
            return _manual_review(idem, identity, submission["draft_id"],
                                  f"回查记录与授权快照不符：{why}", session)
        store.update_submission(idem, "submitted", ticket_no=found["ticket_no"])
        store.set_draft_status(submission["draft_id"], "submitted", actor=identity)
        return {"status": "submitted", "ticket_no": found["ticket_no"], "idem_key": idem,
                "message": "回查确认已受理。"}
    if not found.get("decisive"):
        return JSONResponse({"status": "unknown", "idem_key": idem,
                             "message": "回查暂时失败，不能据此断言已创建或未创建；"
                                        "请稍后用同一标识再查。"}, status_code=202)
    if not submission["authorization_json"]:
        return _manual_review(idem, identity, submission["draft_id"],
                              "历史版本留下的未决提交缺少授权记录，需要人工核对", session)
    return JSONResponse({"status": "not_found", "idem_key": idem,
                         "message": "回查未命中。该提交有完整授权记录，可再次提交同一标识；"
                                    "系统会沿用原请求重放，不会新建第二张单。"}, status_code=200)


@app.get("/api/tickets")
def tickets(authorization: str | None = Header(default=None)):
    session = identity_of(authorization)
    items = [{"ticket_no": t["ticket_no"], "status": t["status"], "asset_id": t["asset_id"],
              "created_at": t["created_at"]} for t in store.user_tickets(session["identity"])]
    return {"status": "ok", "items": items}


@app.get("/api/tickets/{ticket_no}")
def ticket(ticket_no: str, authorization: str | None = Header(default=None)):
    session = identity_of(authorization)
    row = store.get_ticket(ticket_no)
    if row is None or row["user_id"] != session["identity"]:
        raise HTTPException(404, "工单不存在")
    return {"status": "ok", "ticket": {"ticket_no": row["ticket_no"], "status": row["status"],
                                       "asset_id": row["asset_id"], "reason": row["reason"],
                                       "created_at": row["created_at"],
                                       "payload": json.loads(row["payload_json"])}}


@app.get("/")
def index():
    return FileResponse(settings.ROOT / "web" / "index.html")
