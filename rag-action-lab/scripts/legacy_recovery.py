#!/usr/bin/env python3
"""Legacy drill: upgrade a state written by the sealed r2 build and check U2 behaviour.

The unknown record is produced by the r2 code itself (its own fault file delays the write
past the submit timeout), then the r4 build opens the same database. No SQL is edited by hand.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

R4 = Path(__file__).resolve().parent.parent
R2 = Path("/root/rag-lab/rag-action-lab-r2")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code < 500:
                return True
        except Exception:
            time.sleep(0.3)
    return False


def start(root, venv_python, app, port, env, log):
    handle = log.open("ab")
    process = subprocess.Popen([venv_python, "-m", "uvicorn", app, "--host", "127.0.0.1",
                                "--port", str(port), "--no-access-log"],
                               cwd=str(root), env=env, stdout=handle,
                               stderr=subprocess.STDOUT)
    process._log = handle
    return process


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    process._log.close()


def count_tickets(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default="/tmp/a05-legacy")
    parser.add_argument("--evidence", default="/tmp/a05-legacy/evidence")
    args = parser.parse_args()
    state = Path(args.workdir) / "state"
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    if state.exists():
        shutil.rmtree(state)
    (state / "private").mkdir(parents=True)
    env = dict(os.environ)
    env.update({
        "STATE_DIR": str(state), "PRIVATE_DIR": str(state / "private"),
        "DATA_DIR": str(state / "data"), "RUN_ROOT": str(state / "runs"),
        "DB_PATH": str(state / "data" / "action.db"),
        "OGX_BASE": os.environ.get("OGX_BASE", "http://127.0.0.1:8321"),
        "DOC_SERVICE": os.environ.get("DOC_SERVICE", "http://127.0.0.1:8089"),
    })
    subprocess.run([sys.executable, "init_private.py"], cwd=str(R4), env=env,
                   check=True, capture_output=True)
    r2_python = str(R2 / ".venv" / "bin" / "python")
    api_port, mcp_port = free_port(), free_port()
    env_r2 = dict(env, ACTION_MCP_URL=f"http://127.0.0.1:{mcp_port}/mcp",
                  API_PORT=str(api_port), MCP_PORT=str(mcp_port),
                  ACTION_FAULT_FILE=str(state / "data" / "fault-drop-once"),
                  ACTION_FAULT_DELAY="30", SUBMIT_TIMEOUT="8")
    log = evidence / "legacy-recovery.log"
    record = {"r2_build": str(R2), "r4_build": str(R4)}
    mcp = start(R2, r2_python, "mcp_action_server:app", mcp_port, env_r2, log)
    assert wait_http(f"http://127.0.0.1:{mcp_port}/health"), "r2 back office did not start"
    api = start(R2, r2_python, "action_api:app", api_port, env_r2, log)
    base = f"http://127.0.0.1:{api_port}"
    assert wait_http(base + "/api/health"), "r2 API did not start"
    token = json.loads((state / "private" / "credentials-for-delivery.json").read_text())["lab-a"]
    headers = {"Authorization": "Bearer " + token}
    try:
        with httpx.Client(timeout=120) as client:
            ask = client.post(base + "/api/ask", headers=headers,
                              json={"question": "我的笔记本电池鼓包，想申请更换，帮我准备",
                                    "as_of": "2026-03-01"})
            ask.raise_for_status()
            draft = ask.json()["draft"]
            confirmation = client.post(base + f"/api/drafts/{draft['draft_id']}/confirm",
                                       headers=headers,
                                       json={"version": draft["version"],
                                             "content_hash": draft["content_hash"]}).json()
            (state / "data" / "fault-drop-once").write_text("once")
            submit = client.post(base + f"/api/drafts/{draft['draft_id']}/submit",
                                 headers=headers,
                                 json={"confirmation_id": confirmation["confirmation_id"]})
            record["r2_submit"] = {"http": submit.status_code, "body": submit.json()}
            idem = submit.json().get("idem_key")
        time.sleep(35)                              # the delayed write completes now
        record["r2_tickets"] = count_tickets(state / "data" / "action.db")
    finally:
        stop(api)
        stop(mcp)

    # r4 opens the same state: legacy rows have no authorization record.
    subprocess.run([sys.executable, "-c", "import store; store.init(); print('init ok')"],
                   cwd=str(R4), env=env, check=True, capture_output=True)
    env_r4 = dict(env, ACTION_MCP_URL=f"http://127.0.0.1:{mcp_port}/mcp",
                  API_PORT=str(api_port), MCP_PORT=str(mcp_port))
    api = start(R4, str(R4 / ".venv" / "bin" / "python"), "action_api:app", api_port, env_r4, log)
    assert wait_http(base + "/api/health"), "r4 API did not start"
    try:
        with httpx.Client(timeout=120) as client:
            # Back office unreachable: the lookup cannot decide, so nothing may be unlocked.
            down = client.post(base + f"/api/submissions/{idem}/reconcile", headers=headers)
            record["reconcile_without_back_office"] = {"http": down.status_code,
                                                       "body": down.json()}
            retry = client.post(base + f"/api/drafts/{draft['draft_id']}/submit",
                                headers=headers,
                                json={"confirmation_id": confirmation["confirmation_id"]})
            record["submit_without_back_office"] = {"http": retry.status_code,
                                                    "body": retry.json()}
            revise = client.post(base + f"/api/drafts/{draft['draft_id']}/revise",
                                 headers=headers, json={"reason": "换个说法"})
            record["revise_while_frozen"] = revise.status_code
        record["tickets_while_down"] = count_tickets(state / "data" / "action.db")

        mcp = start(R4, str(R4 / ".venv" / "bin" / "python"), "mcp_action_server:app",
                    mcp_port, env_r4, log)
        assert wait_http(f"http://127.0.0.1:{mcp_port}/health"), "r4 back office did not start"
        with httpx.Client(timeout=120) as client:
            after = client.post(base + f"/api/submissions/{idem}/reconcile", headers=headers)
            record["reconcile_after_recovery"] = {"http": after.status_code, "body": after.json()}
        record["tickets_after_recovery"] = count_tickets(state / "data" / "action.db")
    finally:
        stop(api)
        stop(mcp)

    ok = (record["tickets_while_down"] == 1
          and record["tickets_after_recovery"] == 1
          and record.get("revise_while_frozen") == 409)
    record["verdict"] = "pass" if ok else "fail"
    (evidence / "legacy-recovery.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: record[k] for k in ("verdict", "r2_tickets", "tickets_while_down",
                                             "tickets_after_recovery", "revise_while_frozen")},
                     ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
