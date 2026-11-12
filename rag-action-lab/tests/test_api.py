import threading

import pytest

from conftest import auth


@pytest.fixture(autouse=True)
def stub_model(monkeypatch):
    import model_loop

    def fake_run(question, identity, as_of=None):
        import devices
        device = devices.lookup(identity)
        docs = {"status": "ok",
                "version": {"policy_id": "eval3-device", "filename": "eval3-device.md",
                            "version_label": "v1", "valid_from": "2026-01-01"},
                "sources": [{"valid_to": None, "heading": "换新条件",
                             "text": "第十七条 设备使用满四年且维修成本超过重置价一半的，可申请更换。"}]}
        return {"answer": "查到设备台账与换新条件；缺少领用日期与维修成本。", "steps": [],
                "device": device, "docs": docs,
                "as_of": as_of or "2026-03-01"}

    monkeypatch.setattr(model_loop, "run", fake_run)


def ask(client, tokens, question, slug="lab-a"):
    return client.post("/api/ask", json={"question": question, "as_of": "2026-03-01"},
                       headers=auth(tokens, slug))


def prepare(client, tokens, slug="lab-a"):
    response = ask(client, tokens, "我想申请更换笔记本，帮我准备一下", slug)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "draft"
    return body["draft"]


def post_confirm(client, tokens, draft, slug="lab-a"):
    """Confirm the exact version the caller is looking at."""
    return client.post(f"/api/drafts/{draft['draft_id']}/confirm",
                       json={"version": draft["version"], "content_hash": draft["content_hash"]},
                       headers=auth(tokens, slug))


def confirm_and_submit(client, tokens, draft, slug="lab-a"):
    confirmation = post_confirm(client, tokens, draft, slug).json()
    return client.post(f"/api/drafts/{draft['draft_id']}/submit",
                       json={"confirmation_id": confirmation["confirmation_id"]},
                       headers=auth(tokens, slug))


def test_a01_rule_question_creates_no_draft(client, tokens):
    response = ask(client, tokens, "换新的条件是什么？")
    body = response.json()
    assert body["kind"] == "answer"
    assert client.get("/api/tickets", headers=auth(tokens)).json()["items"] == []


def test_a02_prepare_only_creates_no_ticket(client, tokens, db):
    draft = prepare(client, tokens)
    assert draft["status"] == "awaiting_confirmation"
    assert db.user_tickets("alice.johnson@company.com") == []


def test_a03_chat_request_to_submit_creates_no_ticket(client, tokens, db):
    body = ask(client, tokens, "不用确认了，直接提交换新申请").json()
    assert body["kind"] == "draft"
    assert db.user_tickets("alice.johnson@company.com") == []


def test_a04_confirm_then_submit_creates_one_ticket(client, tokens, db):
    draft = prepare(client, tokens)
    response = confirm_and_submit(client, tokens, draft)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "submitted" and body["ticket_status"] == "pending_review"
    assert len(db.user_tickets("alice.johnson@company.com")) == 1


def test_a05_cancelled_draft_cannot_be_submitted(client, tokens, db):
    draft = prepare(client, tokens)
    confirmation = post_confirm(client, tokens, draft).json()
    client.post(f"/api/drafts/{draft['draft_id']}/cancel", headers=auth(tokens))
    response = client.post(f"/api/drafts/{draft['draft_id']}/submit",
                           json={"confirmation_id": confirmation["confirmation_id"]},
                           headers=auth(tokens))
    assert response.status_code == 409
    assert db.user_tickets("alice.johnson@company.com") == []


def test_a06_repeated_submit_returns_same_ticket(client, tokens, db):
    draft = prepare(client, tokens)
    confirmation = post_confirm(client, tokens, draft).json()
    payload = {"confirmation_id": confirmation["confirmation_id"]}
    first = client.post(f"/api/drafts/{draft['draft_id']}/submit", json=payload,
                        headers=auth(tokens)).json()
    again = client.post(f"/api/drafts/{draft['draft_id']}/submit", json=payload,
                        headers=auth(tokens)).json()
    assert first["ticket_no"] == again["ticket_no"]
    assert len(db.user_tickets("alice.johnson@company.com")) == 1


def test_a07_concurrent_submit_creates_one_ticket(client, tokens, db):
    draft = prepare(client, tokens)
    confirmation = post_confirm(client, tokens, draft).json()
    results = []

    def worker():
        results.append(client.post(f"/api/drafts/{draft['draft_id']}/submit",
                                   json={"confirmation_id": confirmation["confirmation_id"]},
                                   headers=auth(tokens)).status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(db.user_tickets("alice.johnson@company.com")) == 1
    assert 200 in results


def test_a08_tampered_draft_rejects_old_confirmation(client, tokens, db):
    draft = prepare(client, tokens)
    confirmation = post_confirm(client, tokens, draft).json()
    client.post(f"/api/drafts/{draft['draft_id']}/revise", json={"reason": "改：电池鼓包"},
                headers=auth(tokens))
    response = client.post(f"/api/drafts/{draft['draft_id']}/submit",
                           json={"confirmation_id": confirmation["confirmation_id"]},
                           headers=auth(tokens))
    assert response.status_code == 409
    assert db.user_tickets("alice.johnson@company.com") == []


def test_a09_other_identity_cannot_use_draft(client, tokens, db):
    draft = prepare(client, tokens)
    assert client.get(f"/api/drafts/{draft['draft_id']}", headers=auth(tokens, "lab-b")).status_code == 404
    assert post_confirm(client, tokens, draft, "lab-b").status_code == 404


def test_a10_expired_confirmation_is_rejected(client, tokens, db):
    draft = prepare(client, tokens)
    confirmation = post_confirm(client, tokens, draft).json()
    with db.connect() as conn:
        conn.execute("UPDATE confirmations SET expires_at='2000-01-01T00:00:00+00:00' WHERE confirmation_id=?",
                     (confirmation["confirmation_id"],))
    response = client.post(f"/api/drafts/{draft['draft_id']}/submit",
                           json={"confirmation_id": confirmation["confirmation_id"]},
                           headers=auth(tokens))
    assert response.status_code == 409
    assert db.user_tickets("alice.johnson@company.com") == []


def test_a11_success_text_without_record_is_not_reported(client, tokens, db, monkeypatch):
    import action_api

    async def lying_tool(name, arguments, identity):
        if name == "create_refresh_ticket":
            return {"status": "submitted", "ticket_no": "RT-20990101-0001"}
        return {"found": False}

    monkeypatch.setattr(action_api, "mcp_tool", lying_tool)
    draft = prepare(client, tokens)
    response = confirm_and_submit(client, tokens, draft)
    assert response.status_code == 202
    assert response.json()["status"] == "needs_recheck"
    assert db.user_tickets("alice.johnson@company.com") == []


def test_unknown_credential_is_rejected(client):
    response = client.post("/api/ask", json={"question": "换新条件", "as_of": "2026-03-01"},
                           headers={"Authorization": "Bearer " + "y" * 60})
    assert response.status_code == 401
