"""Regression tests for the second independent review (S1-S5).

S1 authorized submission: the lock alone must never let a submission skip the
   confirmation check.
S2 canonical binding: the back office derives its fields from the snapshot and
   refuses a payload whose arguments disagree with it.
S3 definite refusal: the user can edit and submit again, exactly once.
S4 stated reason: the user's own words survive into the draft and the ticket.
S5 source selection: the draft keeps the retrieved sources and picks the clause
   that actually talks about conditions instead of the first one.
"""
import json

import pytest

from conftest import auth, echo_lookup


@pytest.fixture(autouse=True)
def stub_model(monkeypatch):
    import model_loop
    import devices

    def fake_run(question, identity, as_of=None):
        device = devices.lookup(identity)
        docs = {"status": "ok",
                "version": {"policy_id": "eval3-device", "filename": "eval3-device.md",
                            "version_label": "v1", "valid_from": "2026-01-01"},
                "sources": [
                    {"chunk_id": "c-unrelated", "valid_to": None, "heading": "第二章 领用条件",
                     "text": "第六条 因岗位调整需要新增设备的，由部门负责人确认职责与使用场景。"},
                    {"chunk_id": "c-condition", "valid_to": None, "heading": "第三章 换新条件",
                     "text": "第十七条 设备使用满四年且维修成本超过重置价一半的，可申请更换。"},
                ]}
        return {"answer": "查到设备台账；缺少领用日期与维修成本。", "steps": [],
                "device": device, "docs": docs, "as_of": as_of or "2026-03-01",
                "user_reason": "电池鼓包" if "电池鼓包" in question else ""}

    monkeypatch.setattr(model_loop, "run", fake_run)


def draft_of(client, tokens, question="我想申请更换笔记本，帮我准备一下"):
    response = client.post("/api/ask", json={"question": question, "as_of": "2026-03-01"},
                           headers=auth(tokens))
    assert response.status_code == 200, response.text
    return response.json()["draft"]


def confirm(client, tokens, draft):
    response = client.post(f"/api/drafts/{draft['draft_id']}/confirm",
                           json={"version": draft["version"],
                                 "content_hash": draft["content_hash"]}, headers=auth(tokens))
    assert response.status_code == 200, response.text
    return response.json()["confirmation_id"]


def submit(client, tokens, draft, confirmation_id):
    return client.post(f"/api/drafts/{draft['draft_id']}/submit",
                       json={"confirmation_id": confirmation_id}, headers=auth(tokens))


def test_s1_empty_or_arbitrary_confirmation_cannot_submit(client, tokens, db):
    draft = draft_of(client, tokens)
    assert client.post(f"/api/drafts/{draft['draft_id']}/submit", json={},
                       headers=auth(tokens)).status_code == 422
    arbitrary = submit(client, tokens, draft, "x" * 24)
    assert arbitrary.status_code == 409 and "确认" in arbitrary.json()["detail"]
    assert db.user_tickets("alice.johnson@company.com") == []
    assert db.get_draft(draft["draft_id"])["status"] == "awaiting_confirmation"


def test_u2_orphan_lock_is_frozen_for_manual_review(client, tokens, db):
    """A lock with no authorization record is never unlocked on a lookup miss.

    Unlocking here is how a legacy record produces a second ticket: the old key still
    points at a write that may exist, and a new confirmation would create another one.
    """
    import store
    key = "orphan-key-" + "o" * 24
    draft = draft_of(client, tokens)
    with db.connect() as conn:
        conn.execute("UPDATE drafts SET status='submitting', idem_key=? WHERE draft_id=?",
                     (key, draft["draft_id"]))
    response = submit(client, tokens, draft, "y" * 24)
    assert response.status_code == 409
    assert "人工核对" in response.json()["detail"]
    assert "恢复为待确认" not in response.json()["detail"]
    assert db.user_tickets("alice.johnson@company.com") == []
    assert db.get_draft(draft["draft_id"])["status"] == "submitted_unknown"
    assert db.get_draft(draft["draft_id"])["idem_key"] == key
    revise = client.post(f"/api/drafts/{draft['draft_id']}/revise",
                         json={"reason": "换个说法"}, headers=auth(tokens))
    assert revise.status_code == 409
    cancel = client.post(f"/api/drafts/{draft['draft_id']}/cancel", headers=auth(tokens))
    assert cancel.status_code == 409


def test_s1_retry_after_unknown_reuses_the_authorized_snapshot(client, tokens, db, monkeypatch):
    import action_api
    calls = {"n": 0, "args": None}

    async def flaky(name, arguments, identity):
        if name != "create_refresh_ticket":
            return echo_lookup(calls["args"], "RT-20261111-6001")
        calls["args"] = dict(arguments)
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("response lost")
        return {"status": "submitted", "ticket_no": "RT-20261111-6001"}

    monkeypatch.setattr(action_api, "mcp_tool", flaky)
    draft = draft_of(client, tokens)
    confirmation_id = confirm(client, tokens, draft)
    first = submit(client, tokens, draft, confirmation_id)
    assert first.status_code == 202 and first.json()["status"] == "needs_recheck"
    second = submit(client, tokens, draft, confirmation_id)
    assert second.status_code == 200 and second.json()["ticket_no"] == "RT-20261111-6001"
    assert db.get_submission(second.json()["idem_key"])["state"] == "submitted"


def test_s3_rejected_draft_can_be_edited_and_submitted_once(client, tokens, db, monkeypatch):
    import action_api

    state = {"n": 0, "args": None}

    async def refuse_then_accept(name, arguments, identity):
        if name != "create_refresh_ticket":
            return echo_lookup(state["args"], "RT-20261111-6002")
        state["args"] = dict(arguments)
        state["n"] += 1
        if state["n"] == 1:
            return {"status": "rejected", "reason": "policy_violation"}
        return {"status": "submitted", "ticket_no": "RT-20261111-6002"}

    monkeypatch.setattr(action_api, "mcp_tool", refuse_then_accept)
    draft = draft_of(client, tokens)
    refused = submit(client, tokens, draft, confirm(client, tokens, draft))
    assert refused.status_code == 422
    assert db.get_draft(draft["draft_id"])["status"] == "awaiting_confirmation"

    revised = client.post(f"/api/drafts/{draft['draft_id']}/revise",
                          json={"reason": "电池鼓包，申请更换"}, headers=auth(tokens))
    assert revised.status_code == 200
    revised_draft = revised.json()["draft"]
    accepted = submit(client, tokens, revised_draft, confirm(client, tokens, revised_draft))
    assert accepted.status_code == 200 and accepted.json()["ticket_no"] == "RT-20261111-6002"
    assert db.get_submission(accepted.json()["idem_key"])["state"] == "submitted"
    assert state["n"] == 2


def test_s3_unknown_outcome_still_freezes_the_draft(client, tokens, db, monkeypatch):
    import action_api

    async def lost(name, arguments, identity):
        if name == "create_refresh_ticket":
            raise TimeoutError("response lost")
        return {"found": False}

    monkeypatch.setattr(action_api, "mcp_tool", lost)
    draft = draft_of(client, tokens)
    assert submit(client, tokens, draft, confirm(client, tokens, draft)).status_code == 202
    response = client.post(f"/api/drafts/{draft['draft_id']}/revise",
                           json={"reason": "换个说法"}, headers=auth(tokens))
    assert response.status_code == 409
    assert db.get_draft(draft["draft_id"])["status"] == "submitted_unknown"


def test_s4_user_reason_is_kept_verbatim(client, tokens, db, monkeypatch):
    import action_api
    sent = {}

    async def capture(name, arguments, identity):
        if name == "create_refresh_ticket":
            sent.update(arguments)
            return {"status": "submitted", "ticket_no": "RT-20261111-6003"}
        return echo_lookup(sent, "RT-20261111-6003")

    monkeypatch.setattr(action_api, "mcp_tool", capture)
    draft = draft_of(client, tokens, "我的笔记本电池鼓包，想申请更换，帮我准备申请")
    assert draft["reason"] == "电池鼓包"
    assert draft["reason_source"] == "user"
    assert submit(client, tokens, draft, confirm(client, tokens, draft)).status_code == 200
    assert sent["reason_text"] == "电池鼓包"
    assert json.loads(sent["content_canonical"])["reason"] == "电池鼓包"


def test_s5_basis_picks_the_matching_source_not_the_first(client, tokens):
    draft = draft_of(client, tokens)
    basis = draft["basis"]
    assert basis["status"] == "retrieved"
    assert basis["chunk_id"] == "c-condition"
    assert "满四年" in basis["condition"]
    assert [item["chunk_id"] for item in basis["sources"]] == ["c-unrelated", "c-condition"]
    assert basis["sources"][0]["excerpt_sha256"] != basis["excerpt_sha256"]


def test_s5_unrelated_sources_are_not_presented_as_a_condition():
    import draft as draft_mod

    basis = draft_mod.basis_from({"version": {"filename": "eval3-device.md"},
                                  "sources": [{"chunk_id": "c1", "text": "第六条 岗位调整需新增设备。"}]})
    assert basis["status"] == "retrieved_unrelated"
    assert basis["condition"] is None
    assert basis["sources"][0]["chunk_id"] == "c1"
    assert "未直接给出" in basis["citation"]
