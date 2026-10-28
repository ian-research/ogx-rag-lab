"""Bounded service readiness checks. Never print credentials or response bodies."""
import argparse
import asyncio
import json
import os
import time
import urllib.request


def http_json(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


async def database_ready():
    import asyncpg
    connection = await asyncpg.connect(
        host="127.0.0.1", port=5432, user="rag_app", database="rag",
        password=os.environ["PGVECTOR_PASSWORD"], timeout=5,
        command_timeout=5,
    )
    try:
        value = await connection.fetchval(
            "SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector"
        )
        return value == 1
    finally:
        await connection.close(timeout=5)


def check(kind):
    if kind == "database":
        return asyncio.run(database_ready())
    if kind == "app":
        data = http_json("http://127.0.0.1:8088/api/health")
        return data.get("api") == "ok" and data.get("worker_alive") is True
    port, expected = {
        "vllm": (8000, {"rag-llm"}),
        "ogx": (8321, {"local-vllm/rag-llm", "cpu-embedding/bge-small-zh"}),
    }[kind]
    data = http_json(f"http://127.0.0.1:{port}/v1/models")
    return expected <= {item["id"] for item in data.get("data", [])}


def wait(kind, timeout, interval=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check(kind):
                print(f"{kind}: ready", flush=True)
                return
        except Exception:
            # Authentication exceptions can contain sensitive data; don't echo them.
            pass
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise SystemExit(f"{kind}: readiness timeout; inspect local service logs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["database", "vllm", "ogx", "app"])
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    wait(args.kind, args.timeout)
