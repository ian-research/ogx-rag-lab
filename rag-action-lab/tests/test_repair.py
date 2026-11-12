"""Regression tests for the issues found in the 2026-11-11 independent review.

Each test names the review item it locks down: R1 (confirmation binding),
R2 (frozen draft / retry), R3 (declared missing reaches the back office),
R4 (no document basis without a retrieved snippet).
"""
import json

import pytest

from conftest import auth, echo_lookup


@pytest.fixture(autouse=True)
def stub_model(monkeypatch):
    import model_loop

    def fake_run(question, identity, as_of=None, docs=True):
        import devices
        device = devices.lookup(identity)
        payload = {"status": "ok",
                   "version": {"policy_id": "eval3-device", "filename": "eval3-device.md",
                               "version_label": "v1", "valid_from": "2026-01-01"},
                   "sources": [{"valid_to": None, "heading": "换新条件",
                                "text": "第十七条 设备使用满四年且维修成本超过重置价一半的，可申请更换。"}]}
        return {"answer": "查到设备台账与换新条件；缺少领用日期与维修成本。", "steps": [],
                "device": device, "docs": payload if docs else {},
                "as_of": as_of or "2026-03-01"}

    monkeypatch.setattr(model_loop, "run", fake_run)


def draft_of(client, tokens, slug="lab-a"):
    response = client.post("/api/ask", json={"question": "我想申请更换笔记本，帮我准备一下",
                                             "as_of": "2026-03-01"}, headers=auth(tokens, slug))
    assert response.status_code == 200, response.text
    return response.json()["draft"]


def confirm(client, tokens, draft, slug="lab-a"):
    return client.post(f"/api/drafts/{draft['draft_id']}/confirm",
                       json={"version": draft["version"], "content_hash": draft["content_hash"]},
                       headers=auth(tokens, slug))


def submit(client, tokens, draft, confirmation_id, slug="lab-a"):
    return client.post(f"/api/drafts/{draft['draft_id']}/submit",
                       json={"confirmation_id": confirmation_id}, headers=auth(tokens, slug))


def test_r1_confirmation_does_not_travel_between_drafts(client, tokens, db):
    """R1: identical content in two drafts must not share one confirmation."""
    first = draft_of(client, tokens)
    second = draft_of(client, tokens)
    # The snapshot carries the draft identity, so two drafts never share a hash even
    # when their visible content is the same.
    assert first["content_hash"] != second["content_hash"]
    confirmation = confirm(client, tokens, first).json()
    response = submit(client, tokens, second, confirmation["confirmation_id"])
    assert response.status_code == 409
    assert "另一份草稿" in response.json()["detail"]
    assert db.user_tickets("alice.johnson@company.com") == []


def test_r1_stale_page_cannot_confirm_a_newer_version(client, tokens, db):
    """R1: a confirmation must name the version the user actually looked at."""
    draft = draft_of(client, tokens)
    stale = {"version": draft["version"], "content_hash": draft["content_hash"]}
    client.post(f"/api/drafts/{draft['draft_id']}/revise", json={"reason": "改：电池鼓包"},
                headers=auth(tokens))
    response = client.post(f"/api/drafts/{draft['draft_id']}/confirm", json=stale,
                           headers=auth(tokens))
    assert response.status_code == 409
    assert "重新预览" in response.json()["detail"]


def test_r2_draft_is_frozen_while_submission_is_unresolved(client, tokens, db, monkeypatch):
    """R2: an unknown result freezes the draft; no edit, no second key."""
    import action_api

    async def lost_reply(name, arguments, identity):
        if name == "create_refresh_ticket":
            raise TimeoutError("simulated lost reply")
        return {"found": False}

    monkeypatch.setattr(action_api, "mcp_tool", lost_reply)
    draft = draft_of(client, tokens)
    confirmation = confirm(client, tokens, draft).json()
    response = submit(client, tokens, draft, confirmation["confirmation_id"])
    assert response.status_code == 202
    assert db.get_draft(draft["draft_id"])["status"] == "submitted_unknown"

    revise = client.post(f"/api/drafts/{draft['draft_id']}/revise", json={"reason": "改：再改一次"},
                         headers=auth(tokens))
    assert revise.status_code == 409
    cancel = client.post(f"/api/drafts/{draft['draft_id']}/cancel", headers=auth(tokens))
    assert cancel.status_code == 409
    again = submit(client, tokens, draft, confirmation["confirmation_id"])
    assert again.status_code == 202
    assert db.user_tickets("alice.johnson@company.com") == []


def test_r2_retry_keeps_one_key_and_creates_one_ticket(client, tokens, db, monkeypatch):
    """R2: retry after a pre-write failure reuses the same identifier."""
    import action_api

    seen = {"keys": [], "fail": True, "args": None}

    async def flaky(name, arguments, identity):
        if name == "create_refresh_ticket":
            seen["keys"].append(arguments["idempotency_key"])
            seen["args"] = dict(arguments)
            if seen["fail"]:
                seen["fail"] = False
                return {"status": "rejected", "reason": "backend_busy"}
            return {"status": "submitted", "ticket_no": "RT-20261111-5001"}
        return echo_lookup(seen["args"], "RT-20261111-5001")

    monkeypatch.setattr(action_api, "mcp_tool", flaky)
    draft = draft_of(client, tokens)
    confirmation = confirm(client, tokens, draft).json()
    first = submit(client, tokens, draft, confirmation["confirmation_id"])
    assert first.status_code == 422          # definite refusal: nothing was written
    assert db.get_draft(draft["draft_id"])["status"] == "awaiting_confirmation"

    second = submit(client, tokens, draft, confirmation["confirmation_id"])
    assert second.status_code == 200 and second.json()["status"] == "submitted"
    assert len(set(seen["keys"])) == 1       # same stable identifier on both attempts
    assert db.get_submission(second.json()["idem_key"])["state"] == "submitted"


def test_r2_cancel_after_submit_is_refused(client, tokens, db):
    """R2: a submitted draft cannot be reported as cancelled."""
    draft = draft_of(client, tokens)
    confirmation = confirm(client, tokens, draft).json()
    accepted = submit(client, tokens, draft, confirmation["confirmation_id"])
    assert accepted.status_code == 200
    response = client.post(f"/api/drafts/{draft['draft_id']}/cancel", headers=auth(tokens))
    assert response.status_code == 409
    assert db.get_draft(draft["draft_id"])["status"] == "submitted"
    assert len(db.user_tickets("alice.johnson@company.com")) == 1


def test_r3_user_declared_missing_reaches_the_ticket(client, tokens, db, monkeypatch):
    """R3: what the user saw as missing must be stored on the accepted ticket."""
    import action_api

    sent = {}

    async def capture(name, arguments, identity):
        sent.update(arguments)
        if name == "create_refresh_ticket":
            return {"status": "submitted", "ticket_no": "RT-20261111-5002"}
        return echo_lookup(sent, "RT-20261111-5002")

    monkeypatch.setattr(action_api, "mcp_tool", capture)
    draft = draft_of(client, tokens)
    assert [item["item"] for item in draft["missing"]] == ["实际使用年限", "维修成本"]
    confirmation = confirm(client, tokens, draft).json()
    assert submit(client, tokens, draft, confirmation["confirmation_id"]).status_code == 200

    assert sent["declared_missing"] == ["实际使用年限", "维修成本"]
    assert sent["doc_snapshot"]["filename"] == "eval3-device.md"
    assert sent["content_hash"] == draft["content_hash"]

    assert sent["reason_text"] == draft["reason"]
    assert json.loads(sent["content_canonical"])["missing"] == draft["missing"]
    rejected = client.post("/api/drafts/" + draft["draft_id"] + "/submit",
                           json={"confirmation_id": confirmation["confirmation_id"]},
                           headers=auth(tokens))
    assert rejected.status_code in (200, 202)  # idempotent replay keeps one ticket


def test_r4_no_basis_citation_without_retrieval(client, tokens, db, monkeypatch):
    """R4: without a retrieved snippet the draft must not quote a clause."""
    import draft as draft_mod

    built = draft_mod.build("alice.johnson@company.com", 1,
                            {"asset_id": "A1", "model": "Latitude 7420",
                             "purchase_date": "2020-01-15", "assigned_date": None}, {}, "理由：电池下降")
    basis = built["basis"]
    assert basis["status"] == "not_retrieved"
    assert basis["condition"] is None
    assert basis["filename"] is None and basis["valid_from"] is None
    assert basis["citation"] == draft_mod.NOT_RETRIEVED
    assert any(item["item"] == "适用文档依据" for item in built["missing"])


def test_r4_submit_payload_carries_the_retrieval_state(client, tokens, db, monkeypatch):
    """R4: the accepted ticket records whether a clause was retrieved, not a default."""
    import action_api

    sent = {}

    async def capture(name, arguments, identity):
        sent.update(arguments)
        if name == "create_refresh_ticket":
            return {"status": "submitted", "ticket_no": "RT-20261111-5003"}
        return {"found": True, "ticket_no": "RT-20261111-5003"}

    monkeypatch.setattr(action_api, "mcp_tool", capture)
    draft = draft_of(client, tokens)
    confirmation = confirm(client, tokens, draft).json()
    submit(client, tokens, draft, confirmation["confirmation_id"])
    assert sent["doc_snapshot"]["status"] == "retrieved"
    assert sent["doc_snapshot"]["excerpt_sha256"]
