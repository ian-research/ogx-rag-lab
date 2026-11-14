import json
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

TMP = Path(tempfile.mkdtemp(prefix="a05-test-"))
os.environ.update({
    "PRIVATE_DIR": str(TMP / "private"),
    "DATA_DIR": str(TMP / "data"),
    "DB_PATH": str(TMP / "data" / "action.db"),
    "RUN_ROOT": str(TMP / "evidence" / "runs"),
    "ACTION_FAULT_FILE": str(TMP / "data" / "fault-drop-once"),
})


def _write_private(name, text):
    path = TMP / "private" / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


@pytest.fixture(scope="session", autouse=True)
def _private():
    import hashlib

    sessions, tokens = [], {}
    for slug, label, identity in [("lab-a", "演示员工 A", "alice.johnson@company.com"),
                                  ("lab-b", "演示员工 B", "john.doe@company.com"),
                                  ("lab-empty", "无设备记录", "no-device@example.invalid")]:
        token = f"test-token-{slug}-" + "x" * 40
        tokens[slug] = token
        sessions.append({"session_id": slug, "label": label, "identity": identity,
                         "token_sha256": hashlib.sha256(token.encode()).hexdigest()})
    _write_private("sessions.json", json.dumps(sessions, ensure_ascii=False))
    _write_private("action.key", "k" * 48)
    _write_private("credentials-for-delivery.json", json.dumps(tokens))
    return tokens


@pytest.fixture(scope="session")
def tokens(_private):
    return _private


@pytest.fixture()
def db():
    import store
    store.init()
    with store.connect() as conn:
        for table in ("drafts", "confirmations", "submissions", "tickets", "audit", "seq"):
            conn.execute(f"DELETE FROM {table}")
    return store


@pytest.fixture()
def client(db, monkeypatch):
    import action_api
    from fastapi.testclient import TestClient

    async def fake_tool(name, arguments, identity):
        if name == "create_refresh_ticket":
            import security
            if not security.verify_proof(arguments["idempotency_key"], arguments["content_hash"],
                                         arguments["proof"]):
                return {"status": "rejected", "reason": "proof_invalid"}
            outcome, row = db.insert_ticket(arguments["idempotency_key"], identity,
                                            arguments["asset_id"], arguments["reason"],
                                            arguments["content_hash"],
                                            {"draft_ref": arguments["draft_ref"],
                                             "version": arguments["version"],
                                             "reason_text": arguments["reason_text"],
                                             "declared_missing": arguments["declared_missing"],
                                             "doc_snapshot": arguments["doc_snapshot"]})
            if outcome == "conflict":
                return {"status": "conflict", "ticket_no": row["ticket_no"]}
            return {"status": "submitted", "ticket_no": row["ticket_no"],
                    "ticket_status": row["status"]}
        if name == "lookup_refresh_ticket":
            row = db.ticket_by_key(arguments["idempotency_key"])
            if row is None or row["user_id"] != identity:
                return {"found": False}
            payload = json.loads(row["payload_json"])
            return {"found": True, "ticket_no": row["ticket_no"], "status": row["status"],
                    "content_hash": row["content_hash"], "asset_id": row["asset_id"],
                    "draft_ref": payload.get("draft_ref"),
                    "version": payload.get("version"),
                    "reason_text": payload.get("reason_text"),
                    "declared_missing": payload.get("declared_missing", []),
                    "doc_snapshot": payload.get("doc_snapshot")}
        raise RuntimeError("unexpected tool")

    monkeypatch.setattr(action_api, "mcp_tool", fake_tool)
    return TestClient(action_api.app)


def auth(tokens, slug="lab-a"):
    return {"Authorization": "Bearer " + tokens[slug]}


def echo_lookup(arguments, ticket_no, status="pending_review", **overrides):
    """An honest back office echoes every protected field the request carried."""
    data = {"found": True, "ticket_no": ticket_no, "status": status,
            "content_hash": arguments["content_hash"], "asset_id": arguments["asset_id"],
            "draft_ref": arguments["draft_ref"], "version": arguments["version"],
            "reason_text": arguments["reason_text"],
            "declared_missing": arguments["declared_missing"],
            "doc_snapshot": arguments["doc_snapshot"]}
    data.update(overrides)
    return data
