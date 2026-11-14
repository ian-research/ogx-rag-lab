"""E1-E6 + L1 + M1 for the article-6 lab (r2).

Every response is written before it is judged; traces are exported as full Jaeger
JSON so a reader can re-check structure without the running backend.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import time
import urllib.request

import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("STATE_DIR", "/root/rag-lab/article06-state")
import store  # noqa: E402

BASE = "http://127.0.0.1:8094"
JAEGER = "http://127.0.0.1:16686"
STATE = pathlib.Path("/root/rag-lab/article06-state")
PRIVATE = STATE / "private"
OUT = STATE / "evidence" / ("e06r2-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
RESULTS = []


def token(name):
    return json.loads((PRIVATE / "credentials-for-delivery.json").read_text())[name]


def client(tok, base=BASE):
    return httpx.Client(base_url=base, timeout=180,
                        headers={"Authorization": f"Bearer {tok}"})


def save(name, payload):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))


def record(item, ok, detail):
    RESULTS.append({"item": item, "pass": bool(ok), "detail": detail})
    save(f"result-{item}", {"item": item, "pass": bool(ok), "detail": detail})
    print(f"[{item}] {'PASS' if ok else 'FAIL'} {json.dumps(detail, ensure_ascii=False)[:300]}", flush=True)


def jaeger(path):
    with urllib.request.urlopen(JAEGER + path, timeout=15) as response:
        return json.loads(response.read().decode())


def export_trace(trace_id, name, min_spans=1):
    """Wait until the batch exporter has flushed, then save the whole trace."""
    previous = -1
    payload = {}
    for _ in range(60):
        try:
            payload = jaeger("/api/traces/" + trace_id)
        except Exception:
            payload = {}
        traces = payload.get("data") or []
        count = sum(len(t.get("spans") or []) for t in traces)
        if count >= min_spans and count == previous:
            break
        previous = count
        time.sleep(1)
    (OUT / f"{name}-{trace_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    return payload


def tree_check(payload):
    traces = payload.get("data") or []
    report = {"traces": len(traces), "spans": 0, "roots": [], "orphans": [], "services": set()}
    for trace in traces:
        spans = trace.get("spans") or []
        processes = trace.get("processes") or {}
        report["spans"] += len(spans)
        ids = {s["spanID"] for s in spans}
        for span in spans:
            proc = processes.get(span.get("processID")) or {}
            report["services"].add(proc.get("serviceName") or "")
            parents = [r["spanID"] for r in span.get("references") or [] if r.get("refType") == "CHILD_OF"]
            if not parents:
                report["roots"].append(span.get("operationName"))
            for parent in parents:
                if parent not in ids:
                    report["orphans"].append(span.get("operationName"))
    report["services"] = sorted(report["services"])
    report["ok"] = report["traces"] == 1 and len(report["roots"]) == 1 and not report["orphans"]
    return report


def clean_links(links):
    """Links are useful for joining; the confirmation id is a capability, not a link."""
    out = {}
    for key, value in (links.get("links") or {}).items():
        item = dict(value or {})
        item.pop("confirmation_id", None)
        out[key] = item
    return out


def tickets_for(draft_id):
    with store.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM tickets "
                           "WHERE json_extract(payload_json, '$.draft_ref') = ?",
                           (draft_id,)).fetchone()
    return row["n"]


def owner_of(draft_id):
    with store.connect() as conn:
        row = conn.execute("SELECT user_id FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
    return row["user_id"] if row else None


def submissions_for(draft_id):
    with store.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM submissions WHERE draft_id=?",
                           (draft_id,)).fetchone()
    return row["n"]


def e1(tok):
    try:
        with client(tok) as c:
            started = time.monotonic()
            response = c.post("/api/ask", json={"question": "我的笔记本是什么型号，什么时候买的？",
                                                "as_of": "2026-03-01"})
            elapsed = round(time.monotonic() - started, 2)
            body = response.json()
        save("e1-response", {"status_code": response.status_code, "elapsed_s": elapsed, "body": body})
        payload = export_trace(body["trace_id"], "e1-trace", min_spans=8)
        tree = tree_check(payload)
        spans = [s for t in payload.get("data") or [] for s in t.get("spans") or []]
        by_name = {}
        for span in spans:
            by_name.setdefault(span["operationName"], []).append(span)
        chats = by_name.get("model.chat", [])
        usage = [{"input": next((t["value"] for t in s["tags"] if t["key"] == "gen_ai.usage.input_tokens"), None),
                  "output": next((t["value"] for t in s["tags"] if t["key"] == "gen_ai.usage.output_tokens"), None),
                  "finish": next((t["value"] for t in s["tags"] if t["key"] == "gen_ai.response.finish_reason"), None)}
                 for s in chats]
        processes = (payload.get("data") or [{}])[0].get("processes") or {}
        versions = sorted({next((t["value"] for t in (p.get("tags") or [])
                                 if t["key"] == "service.version"), "")
                           for p in processes.values()} - {""})
        save("e1-tree", tree)
        ok = (response.status_code == 200 and tree["ok"] and "ask" in by_name
              and all(u["input"] and u["output"] for u in usage))
        ok = ok and bool(versions)
        record("E1", ok, {"trace_id": body["trace_id"], "elapsed_s": elapsed,
                          "tree": tree, "model_rounds": len(chats), "model_usage": usage,
                          "service_version": versions})
    except Exception as exc:  # noqa: BLE001
        record("E1", False, f"{type(exc).__name__}: {exc}")


def e2(tok):
    """One draft, end to end, with the controls that must still hold."""
    steps, ok = {}, False
    try:
        with client(tok) as c:
            ask = c.post("/api/ask", json={"question": "我的笔记本电池鼓包，想申请更换，帮我准备",
                                           "as_of": "2026-03-01"})
            body = ask.json()
            draft = body["draft"]
            steps["prepare"] = {"status": ask.status_code, "trace_id": body.get("trace_id"),
                                "draft_id": draft["draft_id"], "version": draft["version"],
                                "missing": draft["missing"], "reason": draft["reason"],
                                "confirmed_before_preview": c.post(
                                    f"/api/drafts/{draft['draft_id']}/submit",
                                    json={"confirmation_id": "x" * 20}).status_code}
            revise = c.post(f"/api/drafts/{draft['draft_id']}/revise",
                            json={"reason": "电池鼓包，续航不到一小时"})
            revised = revise.json()
            steps["revise"] = {"status": revise.status_code, "version": revised.get("draft", {}).get("version"),
                               "reason": revised.get("draft", {}).get("reason"),
                               "missing": revised.get("draft", {}).get("missing")}
            current = revised["draft"]
            confirm = c.post(f"/api/drafts/{draft['draft_id']}/confirm",
                             json={"version": current["version"],
                                   "content_hash": current["content_hash"]})
            confirmed = confirm.json()
            confirm_ok = (confirm.status_code == 200
                          and bool(confirmed.get("confirmation_id"))
                          and confirmed.get("version") == current["version"])
            steps["confirm"] = {"status": confirm.status_code,
                                "capability_present": bool(confirmed.get("confirmation_id")),
                                "version": confirmed.get("version"),
                                "trace_id": confirmed.get("trace_id"),
                                "ok": confirm_ok}
            stale = c.post(f"/api/drafts/{draft['draft_id']}/confirm",
                           json={"version": current["version"],
                                 "content_hash": "0" * 64})
            steps["stale_confirm"] = {"status": stale.status_code}
            submit = c.post(f"/api/drafts/{draft['draft_id']}/submit",
                            json={"confirmation_id": confirmed["confirmation_id"]})
            submitted = submit.json()
            steps["submit"] = {"status": submit.status_code, "body_status": submitted.get("status"),
                               "ticket_no": submitted.get("ticket_no"),
                               "run_id": submitted.get("run_id")}
            again = c.post(f"/api/drafts/{draft['draft_id']}/submit",
                           json={"confirmation_id": confirmed["confirmation_id"]})
            steps["duplicate_submit"] = {"status": again.status_code,
                                         "ticket_no": again.json().get("ticket_no"),
                                         "same_ticket": again.json().get("ticket_no") == submitted.get("ticket_no")}
            cancel = c.post(f"/api/drafts/{draft['draft_id']}/cancel")
            steps["cancel_after_submit"] = {"status": cancel.status_code}

            # The accepted ticket must carry what the user saw, not a template.
            detail_ok = False
            if submitted.get("ticket_no"):
                detail = c.get(f"/api/tickets/{submitted['ticket_no']}").json()
                row = detail.get("ticket") or detail
                payload = row.get("payload") or {}
                if isinstance(payload, str):
                    payload = json.loads(payload)
                def item_name(value):
                    return value.get("item") if isinstance(value, dict) else str(value)
                got_missing = [item_name(v) for v in payload.get("declared_missing") or []]
                want_missing = [item_name(v) for v in current.get("missing") or []]
                snap = payload.get("doc_snapshot") or {}
                if isinstance(snap, str):
                    snap = json.loads(snap)
                basis = current.get("basis") or {}
                detail_ok = (row.get("reason") == current.get("reason")
                             and got_missing == want_missing
                             and snap.get("status") == basis.get("status")
                             and snap.get("filename") == basis.get("filename"))
                steps["ticket_detail"] = {"reason": row.get("reason"), "missing": got_missing,
                                          "doc_status": snap.get("status"), "doc_file": snap.get("filename"),
                                          "matches_draft": detail_ok}

            # Revising after a confirmation must retire that confirmation.
            second = c.post("/api/ask", json={"question": "我的笔记本电池鼓包，想申请更换，帮我准备",
                                              "as_of": "2026-03-01"}).json()["draft"]
            old_resp = c.post(f"/api/drafts/{second['draft_id']}/confirm",
                              json={"version": second["version"], "content_hash": second["content_hash"]})
            old = old_resp.json() if old_resp.status_code == 200 else {}
            old_capability = bool(old.get("confirmation_id"))
            revise_old = c.post(f"/api/drafts/{second['draft_id']}/revise", json={"reason": "测试改稿后的理由"})
            revised_again = revise_old.json().get("draft", {}) if revise_old.status_code == 200 else {}
            revise_ok = (revise_old.status_code == 200
                         and revised_again.get("version", 0) > second["version"])
            if old_capability:
                retired = c.post(f"/api/drafts/{second['draft_id']}/submit",
                                 json={"confirmation_id": old["confirmation_id"]})
                stale_status = retired.status_code
            else:
                stale_status = None
            steps["revise_retires_confirmation"] = {"old_confirm_status": old_resp.status_code,
                                                    "old_capability_present": old_capability,
                                                    "revise_status": revise_old.status_code,
                                                    "revise_version_after": revised_again.get("version"),
                                                    "stale_submit_status": stale_status}
            # Only an exact 409 after a real capability proves retirement; 422 from an
            # empty or malformed value is not evidence.
            stale_ok = bool(old_capability) and revise_ok and stale_status == 409
            links = clean_links(c.get(f"/api/trace/{draft['draft_id']}").json())
            steps["links"] = {k: (v or {}).get("trace_id") for k, v in links.items()}
        ticket_count = tickets_for(draft["draft_id"])
        submission_count = submissions_for(draft["draft_id"])
        steps["counts"] = {"tickets": ticket_count, "submissions": submission_count,
                           "trace_ids": steps["links"]}
        ids = [v for v in steps["links"].values() if v]
        ok = (steps["prepare"]["status"] == 200
              and steps["prepare"]["confirmed_before_preview"] in (404, 409, 422)
              and steps["revise"]["version"] == 2
              and steps["revise"]["reason"] == "电池鼓包，续航不到一小时"
              and steps["confirm"]["status"] == 200 and steps["confirm"]["ok"]
              and steps["stale_confirm"]["status"] == 409
              and steps["submit"]["status"] == 200 and steps["submit"]["body_status"] == "submitted"
              and steps["duplicate_submit"]["same_ticket"]
              and steps["cancel_after_submit"]["status"] == 409
              and ticket_count == 1 and submission_count == 1 and len(set(ids)) == 3
              and detail_ok and stale_ok)
        for name, trace_id in steps["links"].items():
            if trace_id and name in ("draft", "confirmation", "submission"):
                export_trace(trace_id, f"e2-trace-{name}")
        record("E2", ok, steps)
    except Exception as exc:  # noqa: BLE001
        record("E2", False, {"error": f"{type(exc).__name__}: {exc}", "steps": steps})


def e3(tok):
    """Real HTTP client failure: a separate app instance points at a closed port."""
    proc = None
    tmp = STATE.parent / "article06-docfault"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        (tmp / "private").mkdir(parents=True)
        (tmp / "data").mkdir()
        shutil.copy2(PRIVATE / "sessions.json", tmp / "private" / "sessions.json")
        (tmp / "private" / "action.key").write_text("docfault-key-not-for-production\n")
        env = dict(os.environ,
                   STATE_DIR=str(tmp), PRIVATE_DIR=str(tmp / "private"), DATA_DIR=str(tmp / "data"),
                   DB_PATH=str(tmp / "data" / "action.db"), RUN_ROOT=str(tmp / "runs"),
                   DOC_SERVICE="http://127.0.0.1:9", API_PORT="18094",
                   OTEL_SERVICE_NAME="action-lab-api-docfault", OTEL_ENABLED="1", MCP_PORT="18095")
        proc = subprocess.Popen(
            [".venv/bin/python", "-m", "uvicorn", "action_api:app", "--host", "127.0.0.1",
             "--port", "18094", "--workers", "1", "--no-access-log"],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                if httpx.get("http://127.0.0.1:18094/api/health", timeout=1).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        with client(tok, base="http://127.0.0.1:18094") as c:
            response = c.post("/api/ask", json={"question": "换新的条件是什么", "as_of": "2026-03-01"})
            body = response.json()
        save("e3-docfault-response", {"status_code": response.status_code, "body": body})
        payload = export_trace(body["trace_id"], "e3-trace-docfault", min_spans=3)
        spans = [s for t in payload.get("data") or [] for s in t.get("spans") or []]
        doc = [s for s in spans if s["operationName"] == "doc.search"]
        tags = {t["key"]: t["value"] for t in doc[0]["tags"]} if doc else {}
        ok = (response.status_code == 200 and doc and tags.get("lab.outcome") == "unreachable"
              and tags.get("lab.error_kind", "").startswith("doc_")
              and "error" in str(tags).lower())
        record("E3", ok, {"status_code": response.status_code,
                          "doc_span": {"outcome": tags.get("lab.outcome"),
                                       "error_kind": tags.get("lab.error_kind")},
                          "answer_head": (body.get("answer") or "")[:80],
                          "note": "real closed port, not the fault file"})
    except Exception as exc:  # noqa: BLE001
        record("E3", False, f"{type(exc).__name__}: {exc}")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def _prepare_draft(c, question):
    ask = c.post("/api/ask", json={"question": question, "as_of": "2026-03-01"})
    draft = ask.json()["draft"]
    confirmed = c.post(f"/api/drafts/{draft['draft_id']}/confirm",
                       json={"version": draft["version"],
                             "content_hash": draft["content_hash"]}).json()
    return draft, confirmed


def e4a(tok):
    """MCP unavailable before any write: the ticket must not exist afterwards."""
    try:
        with client(tok) as c:
            draft, confirmed = _prepare_draft(c, "屏幕碎了，想申请更换，帮我准备")
        subprocess.run(["systemctl", "stop", "article06-mcp.service"], check=True, timeout=60)
        time.sleep(1)
        with client(tok) as c:
            submit = c.post(f"/api/drafts/{draft['draft_id']}/submit",
                            json={"confirmation_id": confirmed["confirmation_id"]})
            body = submit.json()
            save("e4a-submit", {"status_code": submit.status_code, "body": body})
        subprocess.run(["systemctl", "start", "article06-mcp.service"], check=True, timeout=60)
        for _ in range(60):
            try:
                if httpx.get("http://127.0.0.1:8095/health", timeout=1).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        with client(tok) as c:
            reconcile = c.post(f"/api/submissions/{body['idem_key']}/reconcile")
            recovered = reconcile.json()
            links = clean_links(c.get(f"/api/trace/{draft['draft_id']}").json())
        if body.get("trace_id"):
            export_trace(body["trace_id"], "e4a-trace-down", min_spans=3)
        count = tickets_for(draft["draft_id"])
        ok = (submit.status_code in (202, 200) and body.get("status") in ("needs_recheck", "submitted")
              and recovered.get("status") in ("not_found", "submitted") and count == (0 if recovered.get("status") == "not_found" else 1))
        record("E4a", ok, {"submit": {"code": submit.status_code, "status": body.get("status")},
                           "reconcile": recovered.get("status"), "tickets_for_draft": count,
                           "links": {k: v.get("trace_id") for k, v in links.items()}})
    except Exception as exc:  # noqa: BLE001
        subprocess.run(["systemctl", "start", "article06-mcp.service"], check=False)
        record("E4a", False, f"{type(exc).__name__}: {exc}")


def e4b(tok):
    """Write landed but the app died before recording: exactly one ticket, recovered by lookup."""
    marker = STATE / "data" / "fault-stage"
    try:
        with client(tok) as c:
            draft, confirmed = _prepare_draft(c, "键盘进水，想申请更换，帮我准备")
        marker.write_text("after-write")
        try:
            with client(tok) as c:
                c.post(f"/api/drafts/{draft['draft_id']}/submit",
                       json={"confirmation_id": confirmed["confirmation_id"]})
        except httpx.HTTPError:
            pass  # the process is expected to die mid-response
        for _ in range(60):
            try:
                if httpx.get(f"{BASE}/api/health", timeout=1).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        marker.unlink(missing_ok=True)
        with store.connect() as conn:
            row = conn.execute("SELECT idem_key FROM submissions WHERE draft_id=? ORDER BY rowid DESC",
                               (draft["draft_id"],)).fetchone()
        idem = row["idem_key"] if row else None
        with client(tok) as c:
            recovered = c.post(f"/api/submissions/{idem}/reconcile").json() if idem else {}
        count = tickets_for(draft["draft_id"])
        ok = bool(idem) and count == 1 and recovered.get("status") in ("submitted", "needs_recheck")
        record("E4b", ok, {"tickets_for_draft": count, "reconcile": recovered.get("status"),
                           "idem_present": bool(idem)})
    except Exception as exc:  # noqa: BLE001
        marker.unlink(missing_ok=True)
        record("E4b", False, f"{type(exc).__name__}: {exc}")


def e5(tok_a, tok_b):
    """Two concurrent requests, two identities: no shared context or draft."""
    import threading
    results = {}

    def ask(name, tok):
        try:
            with client(tok) as c:
                response = c.post("/api/ask", json={"question": "我的笔记本电池鼓包，想申请更换，帮我准备",
                                                    "as_of": "2026-03-01"})
                results[name] = {"status": response.status_code, "body": response.json(),
                                 "trace_header": response.headers.get("x-trace-id")}
        except Exception as exc:  # noqa: BLE001
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}

    threads = [threading.Thread(target=ask, args=("a", tok_a)),
               threading.Thread(target=ask, args=("b", tok_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    save("e5-responses", results)
    try:
        drafts = {k: (v.get("body") or {}).get("draft") or {} for k, v in results.items()}
        ids = {k: d.get("draft_id") for k, d in drafts.items()}
        traces = {k: (v.get("body") or {}).get("trace_id") for k, v in results.items()}
        ok = (all((results[k].get("body") or {}).get("status") == "ok" for k in results)
              and ids["a"] and ids["b"] and ids["a"] != ids["b"]
              and traces["a"] != traces["b"])
        for name, trace_id in traces.items():
            if trace_id:
                export_trace(trace_id, f"e5-trace-{name}")
        owners = {k: owner_of(v) for k, v in ids.items() if v}
        expected = {"a": "alice.johnson@company.com", "b": "john.doe@company.com"}
        owners_ok = owners == expected
        headers = {k: results[k].get("trace_header") for k in results}
        trace_consistent = all(headers[k] and headers[k] == traces[k] for k in traces)
        roots = {}
        for name, trace_id in traces.items():
            if trace_id:
                payload = export_trace(trace_id, f"e5-trace-{name}")
                roots[name] = (payload.get("data") or [{}])[0].get("traceID")
        roots_ok = all(roots.get(k) == traces[k] for k in traces)

        # Cross-identity reads: first prove each identity can read its own draft,
        # so a 404 cannot be a stale or missing id, then read the other's.
        cross = {}
        with client(tok_a) as ca, client(tok_b) as cb:
            own_a = ca.get(f"/api/drafts/{ids['a']}")
            own_b = cb.get(f"/api/drafts/{ids['b']}")
            cross_a = ca.get(f"/api/drafts/{ids['b']}")   # a reads b's draft
            cross_b = cb.get(f"/api/drafts/{ids['a']}")   # b reads a's draft
        cross["own"] = {"a": own_a.status_code, "b": own_b.status_code}
        cross["cross"] = {"a_reads_b": cross_a.status_code, "b_reads_a": cross_b.status_code}
        positive_control = own_a.status_code == 200 and own_b.status_code == 200
        isolated = cross_a.status_code == 404 and cross_b.status_code == 404
        ok = (ok and owners_ok and trace_consistent and roots_ok
              and positive_control and isolated)
        record("E5", ok, {
            "draft_ids": ids, "trace_ids": traces, "owners": owners,
            "expected_owners": expected, "owners_ok": owners_ok,
            "trace_header_matches_body": trace_consistent, "trace_root_matches": roots_ok,
            "cross_read": cross, "positive_control": positive_control, "isolated": isolated,
            "note": "two controlled requests, not a load test",
        })
    except Exception as exc:  # noqa: BLE001
        record("E5", False, f"{type(exc).__name__}: {exc}")


def e6(tok):
    """Trace backend down: export first, business continues, no duplicate, recover after."""
    try:
        with client(tok) as c:
            probe = c.post("/api/ask", json={"question": "我的笔记本是什么型号", "as_of": "2026-03-01"})
            probe_body = probe.json()
        export_trace(probe_body["trace_id"], "e6-trace-before-stop", min_spans=5)
        subprocess.run(["systemctl", "stop", "article06-jaeger.service"], check=True, timeout=120)
        time.sleep(2)
        down = subprocess.run(["ss", "-ltn", "sport = :16686"], capture_output=True, text=True)
        save("e6-jaeger-stopped", {"ss": down.stdout.strip(), "rc": down.returncode})
        with client(tok) as c:
            draft, confirmed = _prepare_draft(c, "屏幕碎了，想申请更换，帮我准备")
            response = c.post(f"/api/drafts/{draft['draft_id']}/submit",
                              json={"confirmation_id": confirmed["confirmation_id"]})
            body = response.json()
        count_one = tickets_for(draft["draft_id"])
        with client(tok) as c:
            again = c.post(f"/api/drafts/{draft['draft_id']}/submit",
                           json={"confirmation_id": confirmed["confirmation_id"]})
        count_two = tickets_for(draft["draft_id"])
        subprocess.run(["systemctl", "start", "article06-jaeger.service"], check=True, timeout=120)
        for _ in range(60):
            try:
                if httpx.get(JAEGER + "/api/services", timeout=1).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        with client(tok) as c:
            after = c.post("/api/ask", json={"question": "我的笔记本是什么型号", "as_of": "2026-03-01"})
            after_body = after.json()
        exported = export_trace(after_body["trace_id"], "e6-trace-after-restart", min_spans=1)
        ok = (probe.status_code == 200 and response.status_code == 200 and body.get("status") == "submitted"
              and count_one == 1 and count_two == 1 and again.json().get("ticket_no") == body.get("ticket_no")
              and bool(exported.get("data")))
        record("E6", ok, {"probe": probe.status_code, "submit": response.status_code,
                          "tickets_after_first": count_one, "tickets_after_repeat": count_two,
                          "same_ticket": again.json().get("ticket_no") == body.get("ticket_no"),
                          "trace_after_restart": bool(exported.get("data")),
                          "note": "traces during the outage are lost; recovery only new requests"})
    except Exception as exc:  # noqa: BLE001
        subprocess.run(["systemctl", "start", "article06-jaeger.service"], check=False)
        record("E6", False, f"{type(exc).__name__}: {exc}")


def l1():
    """Log correlation: three lifecycle events, each with its own trace."""
    try:
        journal = subprocess.run(["journalctl", "-u", "article06-api.service", "--since", "-40min",
                                  "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
        rows = [json.loads(line) for line in journal.splitlines() if line.startswith("{")]
        by_draft = {}
        for row in rows:
            if row.get("draft_id") and row.get("event") in ("draft_prepared", "draft_confirmed",
                                                            "submission_written"):
                by_draft.setdefault(row["draft_id"], []).append(row)
        best = max(by_draft.items(), key=lambda kv: len({r.get("event") for r in kv[1]}), default=(None, []))
        draft_id, events = best
        kinds = {r.get("event") for r in events}
        traces = {r.get("trace_id") for r in events if r.get("trace_id")}
        save("l1-journal", {"draft_id": draft_id, "events": events})
        ok = ({"draft_prepared", "draft_confirmed", "submission_written"} <= kinds
              and len(traces) >= 3 and all("confirmation_id" not in r for r in events))
        record("L1", ok, {"draft_id": draft_id, "events": sorted(kinds),
                          "distinct_traces": len(traces)})
    except Exception as exc:  # noqa: BLE001
        record("L1", False, f"{type(exc).__name__}: {exc}")


def parse_metrics(text):
    """Standard parser when it is installed; callers fall back to line splitting."""
    try:
        from prometheus_client.parser import text_string_to_metric_families
    except ImportError:
        return None
    return list(text_string_to_metric_families(text))


def m1():
    """Metrics: one TYPE per name, real deltas, and a parseable histogram."""
    try:
        def scrape():
            return httpx.get(f"{BASE}/metrics", timeout=10).text
        before = scrape()
        with client(token("lab-a")) as c:
            c.post("/api/ask", json={"question": "我的笔记本是什么型号", "as_of": "2026-03-01"})
        after = scrape()
        (OUT / "m1-metrics-before.txt").write_text(before)
        (OUT / "m1-metrics-after.txt").write_text(after)

        def parse(text):
            types, samples = {}, []
            for line in text.splitlines():
                if line.startswith("# TYPE "):
                    _, _, name, kind = line.split(" ", 3)
                    types[name] = types.get(name, 0) + 1
                elif line and not line.startswith("#"):
                    samples.append(line)
            return types, samples

        families = parse_metrics(after)
        parsed = None
        if families is not None:
            names = [f.name for f in families]
            hist = next((f for f in families if f.name == "actionlab_http_request_duration_seconds"), None)
            buckets = [s for s in (hist.samples if hist else []) if s.name.endswith("_bucket")]
            counts = [s for s in (hist.samples if hist else []) if s.name.endswith("_count")]
            # Buckets are cumulative per label set; compare within each set.
            groups = {}
            for sample in buckets:
                key = tuple(sorted((k, v) for k, v in sample.labels.items() if k != "le"))
                groups.setdefault(key, {})[sample.labels.get("le")] = sample.value
            cum_ok = bool(groups)
            for series in groups.values():
                ordered = [(float(le), value) for le, value in series.items() if le != "+Inf"]
                ordered.sort()
                values = [value for _, value in ordered]
                if any(values[i] > values[i + 1] for i in range(len(values) - 1)):
                    cum_ok = False
                if "+Inf" in series and values and series["+Inf"] < values[-1]:
                    cum_ok = False
            inf_ok = any(s.labels.get("le") == "+Inf" for s in buckets)
            parsed = {"families": len(names), "names": sorted(names), "histogram_buckets": len(buckets),
                      "cumulative": cum_ok, "has_inf": inf_ok, "count_samples": len(counts)}
        types, samples = parse(after)
        requests = [s for s in samples if s.startswith("actionlab_http_requests_total{")]
        def total(text):
            types_, samples_ = parse(text)
            return sum(float(s.rsplit(" ", 1)[1]) for s in samples_
                       if s.startswith("actionlab_http_requests_total{"))
        delta = total(after) - total(before)
        ok = (all(v == 1 for v in types.values()) and delta >= 1
              and any('le="+Inf"' in s for s in samples)
              and not any(s.startswith("actionlab_http_requests_total{route=\"" + "/" + "favicon")
                          for s in samples))
        if parsed is not None:
            ok = ok and parsed["histogram_buckets"] > 0 and parsed["cumulative"] and parsed["has_inf"]
        record("M1", ok, {"type_lines": types, "request_delta": delta, "sample_lines": len(samples),
                          "standard_parser": parsed})
    except Exception as exc:  # noqa: BLE001
        record("M1", False, f"{type(exc).__name__}: {exc}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tok_a, tok_b = token("lab-a"), token("lab-b")
    e4a(tok_a)
    e4b(tok_a)
    e1(tok_a)
    e2(tok_a)
    e3(tok_a)
    e5(tok_a, tok_b)
    e6(tok_a)
    l1()
    m1()
    summary = {"batch": OUT.name,
               "passed": sum(1 for r in RESULTS if r["pass"]), "total": len(RESULTS),
               "items": RESULTS}
    save("summary", summary)
    print(json.dumps({"passed": summary["passed"], "total": summary["total"],
                      "items": [r["item"] for r in RESULTS]}, ensure_ascii=False))
    print("evidence:", OUT)


if __name__ == "__main__":
    main()
