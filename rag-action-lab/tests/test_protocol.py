"""Real MCP protocol tests against the simulated back office on loopback."""
import asyncio
import asyncio.subprocess
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

CODE = Path(__file__).resolve().parents[1]
PORT = 8095
URL = f"http://127.0.0.1:{PORT}/mcp"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mcp")
    env = dict(os.environ)
    env.update({"MCP_PORT": str(PORT), "PRIVATE_DIR": os.environ["PRIVATE_DIR"],
                "DB_PATH": str(tmp / "action.db"), "DATA_DIR": str(tmp),
                "ACTION_FAULT_FILE": str(tmp / "fault")})
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "mcp_action_server:app",
                                "--host", "127.0.0.1", "--port", str(PORT), "--no-access-log"],
                               cwd=str(CODE), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        process.terminate()
        pytest.fail("back office did not start")
    yield process
    process.terminate()
    process.wait(timeout=10)


def _headers(identity="alice.johnson@company.com", key=None):
    import security
    return {"Authorization": "Bearer " + (key or security.service_key()),
            "X-Action-User": identity}


async def _call(tool, arguments, headers):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(URL, headers=headers, sse_read_timeout=20) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
    if result.isError:
        return {"is_error": True, "text": result.content[0].text if result.content else ""}
    return json.loads(result.content[0].text)


def run(coro):
    return asyncio.run(coro)


def _canonical_payload(user, draft, asset="A1", reason="电池续航下降", missing=("维修成本",),
                       basis=None):
    return {"user_id": user, "version": 1, "draft_id": draft,
            "asset": {"asset_id": asset, "model": "Latitude 7420"},
            "reason": reason, "facts": [{"label": "型号", "value": "Latitude 7420"}],
            "basis": basis or {"status": "retrieved", "policy_id": "eval3-device",
                               "filename": "eval3-device.md", "version_label": "v1",
                               "valid_from": "2026-01-01", "valid_to": None,
                               "heading": "第三章", "excerpt_sha256": "a" * 64},
            "missing": [{"item": item, "status": "台账未提供", "why": "未记录"}
                        for item in missing]}


def _args(**extra):
    import hashlib
    import security
    idem = "i" * 32
    draft = "d" * 20
    user = "alice.johnson@company.com"
    payload = extra.pop("payload", None) or _canonical_payload(user, draft)
    payload.update(extra.pop("payload_patch", {}))
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    content = hashlib.sha256(canonical.encode()).hexdigest()
    base = {"idempotency_key": idem, "content_hash": content, "content_canonical": canonical,
            "draft_ref": draft, "asset_id": payload["asset"]["asset_id"],
            "reason": payload["reason"], "reason_text": payload["reason"],
            "declared_missing": [item["item"] for item in payload["missing"]],
            "doc_snapshot": {k: payload["basis"].get(k) for k in
                             ("status", "policy_id", "filename", "version_label",
                              "valid_from", "valid_to", "heading", "excerpt_sha256")}}
    base.update({k: v for k, v in extra.items() if k not in ("proof",)})
    base["proof"] = security.proof_for(base["idempotency_key"], base["content_hash"])
    return base


def test_untrusted_caller_is_rejected(server):
    with pytest.raises(Exception) as error:
        run(_call("create_refresh_ticket", _args(),
                  {"Authorization": "Bearer wrong", "X-Action-User": "alice.johnson@company.com"}))
    message = repr(error.value) + str(error.value)
    assert "401" in message and "Unauthorized" in message


def test_missing_proof_is_rejected(server):
    arguments = _args()
    arguments["proof"] = "0" * 64
    result = run(_call("create_refresh_ticket", arguments, _headers()))
    assert result["status"] == "rejected" and result["reason"] == "proof_invalid"


def test_created_then_duplicate_then_conflict(server):
    arguments = _args()
    first = run(_call("create_refresh_ticket", arguments, _headers()))
    assert first.get("status") == "submitted" and first.get("ticket_no", "").startswith("RT-"), first
    again = run(_call("create_refresh_ticket", arguments, _headers()))
    assert again["status"] == "submitted" and again["ticket_no"] == first["ticket_no"]
    assert again["duplicate"] is True
    other = _args(payload_patch={"reason": "换了内容"})
    conflict = run(_call("create_refresh_ticket", other, _headers()))
    assert conflict["status"] == "conflict"


def test_lookup_and_ownership(server):
    arguments = _args(idempotency_key="j" * 32)
    created = run(_call("create_refresh_ticket", arguments, _headers()))
    found = run(_call("lookup_refresh_ticket", {"idempotency_key": "j" * 32}, _headers()))
    assert found["found"] is True and found["ticket_no"] == created["ticket_no"]
    stranger = run(_call("lookup_refresh_ticket", {"idempotency_key": "j" * 32},
                         _headers("john.doe@company.com")))
    assert stranger["found"] is False
    as_stranger = run(_call("get_refresh_ticket", {"ticket_no": created["ticket_no"]},
                            _headers("john.doe@company.com")))
    missing = run(_call("get_refresh_ticket", {"ticket_no": "RT-19990101-0001"}, _headers()))
    assert as_stranger == missing == {"status": "no_record"}


def test_no_write_tool_takes_plain_confirmation(server):
    """A model-side 'confirmed=true' has no path to a ticket: the write entry needs a proof."""
    arguments = _args(confirmed=True)
    arguments.pop("proof")
    result = run(_call("create_refresh_ticket", arguments, _headers()))
    assert result.get("is_error") is True or result.get("status") == "rejected"


@pytest.mark.parametrize("change,expected", [
    ({"asset_id": "ASSET-ALTERED"}, "content_asset_mismatch"),
    ({"reason": "别的原因"}, "content_reason_mismatch"),
    ({"declared_missing": []}, "content_missing_mismatch"),
    ({"doc_snapshot": {"status": "not_retrieved"}}, "content_source_mismatch"),
    ({"draft_ref": "e" * 20}, "content_draft_mismatch"),
])
def test_payload_fields_must_match_the_canonical_snapshot(server, change, expected):
    """S2: the same canonical bytes and proof, but changed arguments, are refused."""
    arguments = _args(idempotency_key="m" * 32)
    arguments.update(change)
    result = run(_call("create_refresh_ticket", arguments, _headers()))
    assert result["status"] == "rejected" and result["reason"] == expected


def test_canonical_user_must_be_the_caller(server):
    arguments = _args(idempotency_key="n" * 32)
    result = run(_call("create_refresh_ticket", arguments,
                       _headers("john.doe@company.com")))
    assert result["status"] == "rejected" and result["reason"] == "content_user_mismatch"


def test_canonical_must_be_exact_bytes(server):
    arguments = _args(idempotency_key="p" * 32)
    arguments["content_canonical"] = arguments["content_canonical"] + " "
    import hashlib
    arguments["content_hash"] = hashlib.sha256(
        arguments["content_canonical"].encode()).hexdigest()
    import security
    arguments["proof"] = security.proof_for(arguments["idempotency_key"],
                                            arguments["content_hash"])
    result = run(_call("create_refresh_ticket", arguments, _headers()))
    assert result["status"] == "rejected" and result["reason"] == "content_not_canonical"
