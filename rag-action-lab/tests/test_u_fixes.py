"""Regression tests for the third independent review (U1-U3).

U1 an authorized submission that stopped mid-flight can be resumed under its own key
U2 legacy records without an authorization record are frozen, never unlocked on a miss
U3 the lookup response must be complete and equal to the frozen request
"""
import json

import pytest

from conftest import auth, echo_lookup


@pytest.fixture(autouse=True)
def stub_model(monkeypatch):
    import devices
    import model_loop

    def fake_run(question, identity, as_of=None):
        docs = {"status": "ok",
                "version": {"policy_id": "eval3-device", "filename": "eval3-device.md",
                            "version_label": "v1", "valid_from": "2026-01-01"},
                "sources": [{"chunk_id": "c-condition", "valid_to": None, "heading": "第十七条",
                             "text": "第十七条 设备使用满四年且维修成本超过重置价一半的，可申请更换。"}]}
        return {"answer": "查到设备台账与换新条件。", "steps": [],
                "device": devices.lookup(identity), "docs": docs,
                "as_of": as_of or "2026-03-01", "user_reason": None}

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


def _lock(client, tokens, db, monkeypatch, *, save_request):
    """Reproduce a process stop right after the lock (and optionally the request)."""
    import action_api
    import store

    draft = draft_of(client, tokens)
    confirmation = confirm(client, tokens, draft)
    idem = action_api.idempotency_key("alice.johnson@company.com", draft["draft_id"],
                                      draft["version"], draft["content_hash"])
    locked, _, error = store.authorize_and_lock(draft["draft_id"],
                                                "alice.johnson@company.com", idem, confirmation)
    assert error is None
    arguments = action_api._arguments_from_row(locked, idem)
    if save_request:
        store.set_submission_request(idem, arguments)
    # The lease of the dead process must expire before another caller may take over.
    with db.connect() as conn:
        conn.execute("UPDATE submissions SET lease_until='2000-01-01T00:00:00+00:00' "
                     "WHERE idem_key=?", (idem,))
    return draft, confirmation, idem, arguments


def test_u1_interrupt_after_authorization_still_reaches_one_ticket(client, tokens, db, monkeypatch):
    async def honest(name, arguments, identity):
        if name == "create_refresh_ticket":
            outcome, row = db.insert_ticket(arguments["idempotency_key"], identity,
                                            arguments["asset_id"], arguments["reason"],
                                            arguments["content_hash"], arguments)
            return {"status": "submitted" if outcome == "created" else "duplicate",
                    "ticket_no": row["ticket_no"]}
        row = db.ticket_by_key(arguments["idempotency_key"])
        return {"found": False} if row is None else echo_lookup(frozen, row["ticket_no"])

    draft, confirmation, idem, frozen = _lock(client, tokens, db, monkeypatch, save_request=False)
    monkeypatch.setattr("action_api.mcp_tool", honest)
    response = submit(client, tokens, draft, confirmation)
    assert response.status_code == 200, response.text
    assert response.json()["ticket_no"].startswith("RT-")
    assert len(db.user_tickets("alice.johnson@company.com")) == 1
    assert db.get_submission(idem)["state"] == "submitted"


def test_u1_interrupt_after_request_is_replayed_not_rebuilt(client, tokens, db, monkeypatch):
    seen = []

    async def honest(name, arguments, identity):
        if name == "create_refresh_ticket":
            seen.append(json.dumps(arguments, sort_keys=True))
            outcome, row = db.insert_ticket(arguments["idempotency_key"], identity,
                                            arguments["asset_id"], arguments["reason"],
                                            arguments["content_hash"], arguments)
            return {"status": "submitted" if outcome == "created" else "duplicate",
                    "ticket_no": row["ticket_no"]}
        row = db.ticket_by_key(arguments["idempotency_key"])
        return {"found": False} if row is None else echo_lookup(frozen, row["ticket_no"])

    draft, confirmation, idem, frozen = _lock(client, tokens, db, monkeypatch, save_request=True)
    monkeypatch.setattr("action_api.mcp_tool", honest)
    response = submit(client, tokens, draft, confirmation)
    assert response.status_code == 200, response.text
    assert len(db.user_tickets("alice.johnson@company.com")) == 1
    assert len(seen) == 1
    assert json.loads(seen[0]) == frozen         # byte-identical replay, not rebuilt


def test_u1_interrupt_after_write_is_found_by_lookup(client, tokens, db, monkeypatch):
    draft, confirmation, idem, frozen = _lock(client, tokens, db, monkeypatch, save_request=True)
    db.insert_ticket(frozen["idempotency_key"], "alice.johnson@company.com",
                     frozen["asset_id"], frozen["reason"], frozen["content_hash"], frozen)

    async def lookup_only(name, arguments_, identity):
        if name == "create_refresh_ticket":
            raise AssertionError("a written ticket must be found, not rewritten")
        row = db.ticket_by_key(arguments_["idempotency_key"])
        return {"found": False} if row is None else echo_lookup(frozen, row["ticket_no"])

    monkeypatch.setattr("action_api.mcp_tool", lookup_only)
    response = submit(client, tokens, draft, confirmation)
    assert response.status_code == 200, response.text
    assert len(db.user_tickets("alice.johnson@company.com")) == 1


def test_u1_live_lease_is_not_stolen(client, tokens, db, monkeypatch):
    async def honest(name, arguments, identity):
        if name == "create_refresh_ticket":
            return {"status": "submitted", "ticket_no": "RT-20990101-0001"}
        return {"found": False}

    monkeypatch.setattr("action_api.mcp_tool", honest)
    draft, confirmation, idem, frozen = _lock(client, tokens, db, monkeypatch, save_request=True)
    with db.connect() as conn:                      # a live attempt holds a fresh lease
        conn.execute("UPDATE submissions SET lease_until=? WHERE idem_key=?",
                     ("2999-01-01T00:00:00+00:00", idem))
    response = submit(client, tokens, draft, confirmation)
    assert response.status_code == 202
    assert response.json()["status"] == "submitting"
    assert db.user_tickets("alice.johnson@company.com") == []


def test_u2_legacy_record_without_authorization_is_not_unlocked(client, tokens, db, monkeypatch):
    draft = draft_of(client, tokens)
    idem = "legacy-" + "k" * 32
    with db.connect() as conn:
        conn.execute("UPDATE drafts SET status='submitting', idem_key=? WHERE draft_id=?",
                     (idem, draft["draft_id"]))
        conn.execute("INSERT INTO submissions(idem_key,user_id,draft_id,version,content_hash,"
                     "confirmation_id,state,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,'submitted_unknown',?,?)",
                     (idem, "alice.johnson@company.com", draft["draft_id"], draft["version"],
                      draft["content_hash"], "legacy-confirmation",
                      "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"))

    async def no_record(name, arguments, identity):
        return {"found": False}

    monkeypatch.setattr("action_api.mcp_tool", no_record)
    response = submit(client, tokens, draft, "z" * 24)
    assert response.status_code == 409
    assert "人工核对" in response.json()["detail"]
    assert db.user_tickets("alice.johnson@company.com") == []
    assert db.get_draft(draft["draft_id"])["status"] == "submitted_unknown"
    assert db.get_draft(draft["draft_id"])["idem_key"] == idem
    assert db.get_submission(idem)["state"] == "manual_review"


def test_u2_unknown_lookup_keeps_the_freeze(client, tokens, db, monkeypatch):
    async def broken(name, arguments, identity):
        if name == "create_refresh_ticket":
            return {"status": "submitted", "ticket_no": "RT-20990101-0002"}
        raise TimeoutError("lookup timed out")

    monkeypatch.setattr("action_api.mcp_tool", broken)
    draft = draft_of(client, tokens)
    confirmation = confirm(client, tokens, draft)
    assert submit(client, tokens, draft, confirmation).status_code == 202
    again = submit(client, tokens, draft, confirmation)
    assert again.status_code == 202
    assert again.json()["status"] == "recovering_unknown"
    assert db.get_draft(draft["draft_id"])["status"] == "submitted_unknown"
    revise = client.post(f"/api/drafts/{draft['draft_id']}/revise",
                         json={"reason": "换个说法"}, headers=auth(tokens))
    assert revise.status_code == 409


@pytest.mark.parametrize("mutation", ["drop_hash", "drop_draft", "wrong_reason",
                                      "wrong_missing", "wrong_doc", "wrong_version"])
def test_u3_incomplete_or_mismatched_lookup_is_not_accepted(client, tokens, db, monkeypatch, mutation):
    sent = {}

    async def tampering(name, arguments, identity):
        if name == "create_refresh_ticket":
            sent.update(arguments)
            return {"status": "submitted", "ticket_no": "RT-20990101-0003"}
        answer = echo_lookup(sent, "RT-20990101-0003")
        if mutation == "drop_hash":
            answer.pop("content_hash")
        elif mutation == "drop_draft":
            answer.pop("draft_ref")
        elif mutation == "wrong_reason":
            answer["reason_text"] = "别的理由"
        elif mutation == "wrong_missing":
            answer["declared_missing"] = []
        elif mutation == "wrong_doc":
            answer["doc_snapshot"] = dict(answer["doc_snapshot"], heading="别的条款")
        elif mutation == "wrong_version":
            answer["version"] = (answer["version"] or 1) + 1
        return answer

    monkeypatch.setattr("action_api.mcp_tool", tampering)
    draft = draft_of(client, tokens)
    response = submit(client, tokens, draft, confirm(client, tokens, draft))
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "needs_recheck"
    assert len(db.user_tickets("alice.johnson@company.com")) == 0
