import threading

import pytest


def _built(store, draft_mod, email="alice.johnson@company.com", version=1):
    import devices
    device = devices.lookup(email)
    docs = {"version": {"filename": "eval3-device.md", "version_label": "v1",
                        "valid_from": "2026-01-01", "policy_id": "eval3-device"},
            "sources": [{"valid_to": None, "text": "第十七条 设备使用满四年…", "heading": "换新条件"}]}
    return draft_mod.build(email, version, device, docs, "理由：电池续航明显下降")


def test_draft_and_confirmation_binding(db):
    import draft as draft_mod
    row = db.create_draft(_built(db, draft_mod))
    assert row["status"] == "awaiting_confirmation"
    assert row["content_hash"] and row["expires_at"].endswith("+00:00")
    confirmation = db.create_confirmation(row, "c" * 32)
    assert confirmation["version"] == row["version"]
    assert confirmation["content_hash"] == row["content_hash"]


def test_revise_supersedes_confirmation(db):
    import draft as draft_mod
    row = db.create_draft(_built(db, draft_mod))
    first = db.create_confirmation(row, "a" * 32)
    text, new_hash = draft_mod.rehash(row, "理由：改为电池鼓包")
    updated, error = db.revise_draft(row["draft_id"], row["user_id"], "理由：改为电池鼓包",
                                     new_hash, text)
    assert error is None
    assert updated["version"] == row["version"] + 1
    assert updated["content_hash"] != row["content_hash"]
    assert db.get_confirmation(first["confirmation_id"])["status"] == "superseded"


def test_cancel_blocks_further_change(db):
    import draft as draft_mod
    row = db.create_draft(_built(db, draft_mod))
    cancelled, error = db.cancel_draft(row["draft_id"], row["user_id"])
    assert error is None
    assert cancelled["status"] == "cancelled"
    frozen, error = db.revise_draft(row["draft_id"], row["user_id"], "新理由", "h" * 64, "{}")
    assert frozen is None and error == "cancelled"


def test_ticket_created_duplicate_conflict(db):
    payload = {"draft_ref": "d1"}
    outcome, row = db.insert_ticket("k" * 32, "alice.johnson@company.com", "A1", "r", "h" * 64, payload)
    assert outcome == "created" and row["status"] == "pending_review"
    assert row["ticket_no"].startswith("RT-")
    again, same = db.insert_ticket("k" * 32, "alice.johnson@company.com", "A1", "r", "h" * 64, payload)
    assert again == "duplicate" and same["ticket_no"] == row["ticket_no"]
    conflict, kept = db.insert_ticket("k" * 32, "alice.johnson@company.com", "A1", "r", "x" * 64, payload)
    assert conflict == "conflict" and kept["ticket_no"] == row["ticket_no"]
    assert len(db.user_tickets("alice.johnson@company.com")) == 1


def test_concurrent_insert_creates_one_ticket(db):
    results = []

    def worker():
        results.append(db.insert_ticket("z" * 32, "alice.johnson@company.com", "A1", "r",
                                        "h" * 64, {})[0])

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("created") == 1
    assert results.count("duplicate") == 5
    assert len(db.user_tickets("alice.johnson@company.com")) == 1


def test_canonical_text_matches_hash(db):
    """The stored canonical text must hash to the stored content hash."""
    import hashlib
    import draft as draft_mod
    row = db.create_draft(_built(db, draft_mod))
    assert hashlib.sha256(row["canonical_json"].encode()).hexdigest() == row["content_hash"]
    text, new_hash = draft_mod.rehash(row, "理由：改为电池鼓包")
    updated, _ = db.revise_draft(row["draft_id"], row["user_id"], "理由：改为电池鼓包",
                                 new_hash, text)
    assert hashlib.sha256(updated["canonical_json"].encode()).hexdigest() == updated["content_hash"]
