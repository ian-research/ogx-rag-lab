"""E7: what does collection cost? Exploratory, not a benchmark.

Two fresh processes on the same code, same scratch state copy, one with
OTEL_ENABLED=0 and one with OTEL_ENABLED=1. Five asks each, after one warm-up.
Reports wall time, process CPU time and RSS, and the model token counters that
the process itself exposes. Small sample: treat the numbers as a direction.
"""
import json
import os
import pathlib
import shutil
import subprocess
import time

import httpx

STATE = pathlib.Path("/root/rag-lab/article06-state")
OUT = STATE / "evidence" / ("e07-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 18096
QUESTIONS = ["我的笔记本是什么型号", "购买日期是哪天", "领用日期是哪天",
             "保修什么时候到期", "资产编号是多少"]


def token():
    creds = json.loads((STATE / "private" / "credentials-for-delivery.json").read_text())
    return creds["lab-a"]


def proc_stats(pid):
    stat = pathlib.Path(f"/proc/{pid}/stat").read_text().split()
    cpu = (int(stat[13]) + int(stat[14])) / os.sysconf("SC_CLK_TCK")
    rss = 0
    for line in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1])
    return cpu, rss


def run(enabled):
    tmp = pathlib.Path(f"/root/rag-lab/article06-e07-{'on' if enabled else 'off'}")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "private").mkdir(parents=True)
    (tmp / "data").mkdir()
    shutil.copy2(STATE / "private" / "sessions.json", tmp / "private" / "sessions.json")
    (tmp / "private" / "action.key").write_text("e07-key-not-for-production\n")
    env = dict(os.environ, STATE_DIR=str(tmp), PRIVATE_DIR=str(tmp / "private"),
               DATA_DIR=str(tmp / "data"), DB_PATH=str(tmp / "data" / "action.db"),
               RUN_ROOT=str(tmp / "runs"), API_PORT=str(PORT),
               OTEL_ENABLED="1" if enabled else "0",
               OTEL_SERVICE_NAME="action-lab-e07", MCP_PORT="18097")
    proc = subprocess.Popen([".venv/bin/python", "-m", "uvicorn", "action_api:app",
                             "--host", "127.0.0.1", "--port", str(PORT), "--workers", "1",
                             "--no-access-log"], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{PORT}/api/health", timeout=1).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        headers = {"Authorization": f"Bearer {token()}"}
        with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=180, headers=headers) as client:
            # Warm-up is measured separately and never enters the sample.
            warm = client.post("/api/ask", json={"question": "我的笔记本是什么型号", "as_of": "2026-03-01"})
            warm_ok = warm.status_code == 200 and (warm.json() or {}).get("status") == "ok"
            time.sleep(1)
            cpu0, rss0 = proc_stats(proc.pid)
            prompt0 = completion0 = 0.0
            for line in client.get("/metrics").text.splitlines():
                if line.startswith("actionlab_model_prompt_tokens_total"):
                    prompt0 = float(line.rsplit(" ", 1)[1])
                if line.startswith("actionlab_model_completion_tokens_total"):
                    completion0 = float(line.rsplit(" ", 1)[1])
            durations, samples = [], []
            for question in QUESTIONS:
                started = time.monotonic()
                response = client.post("/api/ask", json={"question": question, "as_of": "2026-03-01"})
                elapsed = round(time.monotonic() - started, 3)
                body = {}
                try:
                    body = response.json() or {}
                except ValueError:
                    body = {}
                samples.append({"question": question, "http": response.status_code,
                                "status": body.get("status"), "trace_id": body.get("trace_id"),
                                "has_trace": bool(body.get("trace_id")), "duration_s": elapsed})
                durations.append(elapsed)
            cpu1, rss1 = proc_stats(proc.pid)
            metrics = client.get("/metrics").text
        # Business validity is HTTP 200 + status ok. A trace id is expected when
        # collection is on and expected to be absent when it is off; that is a
        # switch check, not a sample filter.
        valid = [s for s in samples if s["http"] == 200 and s["status"] == "ok"]
        dropped = [s for s in samples if s not in valid]
        with_trace = [s for s in valid if s["has_trace"]]
        trace_check = {"expected": enabled, "with_trace": len(with_trace),
                       "ids": sorted({s["trace_id"] for s in with_trace if s["trace_id"]})}
        tokens = {}
        for line in metrics.splitlines():
            if line.startswith("actionlab_model_prompt_tokens_total"):
                tokens["prompt"] = float(line.rsplit(" ", 1)[1])
            if line.startswith("actionlab_model_completion_tokens_total"):
                tokens["completion"] = float(line.rsplit(" ", 1)[1])
        valid_durations = [s["duration_s"] for s in valid]
        return {"enabled": enabled, "warmup_ok": warm_ok,
                "requests": len(samples), "valid": len(valid), "dropped": dropped,
                "durations_s": valid_durations,
                "wall_s": round(sum(valid_durations), 3),
                "mean_s": round(sum(valid_durations) / len(valid_durations), 3) if valid_durations else None,
                "cpu_s": round(cpu1 - cpu0, 2), "rss_kb_start": rss0, "rss_kb_end": rss1,
                "rss_delta_kb": rss1 - rss0,
                "trace_check": trace_check,
                "tokens": tokens, "tokens_measured": {
                    "prompt": (tokens.get("prompt") or 0) - prompt0,
                    "completion": (tokens.get("completion") or 0) - completion0},
                "samples": samples}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    result = {"batch": OUT.name,
              "note": "n=5 after one warm-up; 200/ok responses count as samples; trace presence is checked separately; exploratory",
              "runs": [run(False), run(True)]}
    (OUT / "e07-collection.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for item in result["runs"]:
        print(json.dumps(item, ensure_ascii=False))
    print("evidence:", OUT)


if __name__ == "__main__":
    main()
