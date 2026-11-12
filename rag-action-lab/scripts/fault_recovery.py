#!/usr/bin/env python3
"""Fault-recovery drill: stop the API at a persistence boundary, then resume the same key.

Each stage runs the real API and the real simulated back office as separate processes, on a
scratch state directory, so the interruption is a process death, not a mock.
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

ROOT = Path(__file__).resolve().parent.parent
STAGES = ("after-authorize", "after-request", "after-write")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code < 500:
                return True
        except Exception:
            time.sleep(0.2)
    return False


def start(app, port, env, log):
    handle = log.open("ab")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port),
         "--no-access-log"],
        cwd=str(ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT)
    process._log = handle            # keep the handle alive for the process lifetime
    return process


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    process._log.close()


def token_of(private_dir):
    data = json.loads((Path(private_dir) / "credentials-for-delivery.json").read_text())
    return data["lab-a"]


def tickets_for(db_path, draft_ref):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT COUNT(*) FROM tickets WHERE payload_json LIKE ?",
                           (f"%{draft_ref}%",)).fetchone()
        return row[0]
    finally:
        conn.close()


def run_stage(stage, workdir, evidence):
    state = workdir / stage
    if state.exists():
        shutil.rmtree(state)
    (state / "private").mkdir(parents=True)
    env = dict(os.environ)
    env.update({
        "STATE_DIR": str(state),
        "PRIVATE_DIR": str(state / "private"),
        "DATA_DIR": str(state / "data"),
        "RUN_ROOT": str(state / "runs"),
        "DB_PATH": str(state / "data" / "action.db"),
        "OGX_BASE": os.environ.get("OGX_BASE", "http://127.0.0.1:8321"),
        "DOC_SERVICE": os.environ.get("DOC_SERVICE", "http://127.0.0.1:8089"),
    })
    subprocess.run([sys.executable, "init_private.py"], cwd=str(ROOT), env=env,
                   check=True, capture_output=True)
    api_port, mcp_port = free_port(), free_port()
    env["ACTION_MCP_URL"] = f"http://127.0.0.1:{mcp_port}/mcp"
    env["PUBLIC_BASE"] = f"http://127.0.0.1:{api_port}"
    env["API_PORT"] = str(api_port)
    env["MCP_PORT"] = str(mcp_port)
    log = (evidence / f"fault-{stage}.log")
    mcp = start("mcp_action_server:app", mcp_port, env, log)
    assert wait_http(f"http://127.0.0.1:{mcp_port}/health"), "back office did not start"
    api_env = dict(env, ACTION_FAULT_STAGE=stage)
    api = start("action_api:app", api_port, api_env, log)
    base = f"http://127.0.0.1:{api_port}"
    assert wait_http(base + "/api/health"), "API did not start"
    headers = {"Authorization": "Bearer " + token_of(state / "private")}
    record = {"stage": stage}
    try:
        with httpx.Client(timeout=120) as client:
            ask = client.post(base + "/api/ask", headers=headers,
                              json={"question": "我的笔记本电池鼓包，想申请更换，帮我准备",
                                    "as_of": "2026-03-01"})
            ask.raise_for_status()
            draft = ask.json()["draft"]
            record["draft"] = {"draft_id": draft["draft_id"], "version": draft["version"],
                               "content_hash": draft["content_hash"]}
            confirmed = client.post(base + f"/api/drafts/{draft['draft_id']}/confirm",
                                    headers=headers,
                                    json={"version": draft["version"],
                                          "content_hash": draft["content_hash"]})
            confirmed.raise_for_status()
            confirmation = confirmed.json()["confirmation_id"]
            record["confirmation_id"] = confirmation
            try:
                first = client.post(base + f"/api/drafts/{draft['draft_id']}/submit",
                                    headers=headers, json={"confirmation_id": confirmation})
                record["interrupted"] = {"http": first.status_code,
                                          "body": first.json() if first.content else None}
            except Exception as error:      # the process died at the boundary
                record["interrupted"] = {"http": None, "error": type(error).__name__}
        assert api.poll() is not None, "the fault hook did not stop the API"
        code = api.wait(timeout=30)
        record["exit_code"] = code
        api._log.close()

        # Same code, no fault: the user retries with the very same confirmation.
        api = start("action_api:app", api_port, env, log)
        assert wait_http(base + "/api/health"), "API did not come back"
        attempts = []
        deadline = time.time() + 170          # the lease is bounded at 120 s
        with httpx.Client(timeout=120) as client:
            while True:
                retry = client.post(base + f"/api/drafts/{draft['draft_id']}/submit",
                                    headers=headers, json={"confirmation_id": confirmation})
                attempts.append({"http": retry.status_code,
                                 "status": retry.json().get("status")})
                if retry.status_code == 200 or time.time() > deadline:
                    break
                time.sleep(8)                 # wait for the dead attempt's lease to expire
            record["attempts"] = attempts
            record["retry"] = {"http": retry.status_code, "body": retry.json()}
            again = client.post(base + f"/api/drafts/{draft['draft_id']}/submit",
                                headers=headers, json={"confirmation_id": confirmation})
            record["second_retry"] = {"http": again.status_code,
                                       "ticket_no": again.json().get("ticket_no")}
        record["tickets_for_draft"] = tickets_for(state / "data" / "action.db",
                                                  draft["draft_id"])
    finally:
        stop(api)
        stop(mcp)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default="/tmp/a05-fault")
    parser.add_argument("--evidence", default="/tmp/a05-fault/evidence")
    parser.add_argument("--stages", default=",".join(STAGES))
    args = parser.parse_args()
    workdir = Path(args.workdir)
    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    results = []
    for stage in [item for item in args.stages.split(",") if item]:
        result = run_stage(stage, workdir, evidence)
        results.append(result)
        ok = (result.get("tickets_for_draft") == 1
              and result["retry"]["http"] == 200
              and result["second_retry"].get("ticket_no") == result["retry"]["body"].get("ticket_no"))
        result["verdict"] = "pass" if ok else "fail"
        print(json.dumps({k: result[k] for k in ("stage", "verdict", "exit_code",
                                                 "tickets_for_draft")}, ensure_ascii=False))
    (evidence / "fault-recovery.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(item["verdict"] == "pass" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
