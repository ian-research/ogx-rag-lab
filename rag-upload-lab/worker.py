"""独立 CPU Worker；持久队列、单进程锁、任务子进程及超时控制。"""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ.setdefault("OMP_NUM_THREADS", "4")

from settings import DATA, TOKENIZER, EMBED_MODEL, DIM, ACTIVE
import state
from ogx_client import client, call


def process(job_id):
    job = state.get(job_id)
    if not job or job["status"] not in ACTIVE:
        raise RuntimeError("任务未被 Worker 领取")
    directory = DATA / "artifacts" / job_id
    directory.mkdir(exist_ok=True)
    chunk_path = directory / "chunks.json"
    if chunk_path.exists():
        # 使用冻结的片段重试，避免部分成功后片段 ID 变化或残留旧片段。
        chunks = json.loads(chunk_path.read_text(encoding="utf-8"))
    else:
        from parsing import parse
        from chunking import make_chunks
        from transformers import AutoTokenizer
        state.update(job_id, status="parsing")
        blocks = parse(job["path"])
        (directory / "blocks.json").write_text(json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8")
        state.update(job_id, status="chunking")
        tokenizer = AutoTokenizer.from_pretrained(TOKENIZER, local_files_only=True, trust_remote_code=False)
        chunks = make_chunks(blocks, tokenizer, job_id, job["filename"], job["sha256"])
        if not chunks:
            raise RuntimeError("没有可入库片段")
        temporary = chunk_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(chunk_path)
    state.update(job_id, status="embedding", total=len(chunks), done=0)
    with client() as c:
        store_id = job["store_id"]
        if not store_id:
            created = call(c, "POST", "/vector_stores", {
                "name": "upload-" + job_id, "provider_id": "local-pgvector",
                "embedding_model": EMBED_MODEL, "embedding_dimension": DIM,
            })
            store_id = created["id"]
            state.update(job_id, store_id=store_id)
        else:
            call(c, "GET", f"/vector_stores/{store_id}")
        for start in range(0, len(chunks), 16):
            batch = chunks[start:start + 16]
            result = call(c, "POST", "/embeddings", {
                "model": EMBED_MODEL, "input": [chunk["content"] for chunk in batch],
                "encoding_format": "float",
            })
            vectors = sorted(result["data"], key=lambda item: item["index"])
            if [item["index"] for item in vectors] != list(range(len(batch))):
                raise RuntimeError("Embedding 返回数量或索引不匹配")
            embedded = []
            for chunk, vector in zip(batch, vectors):
                if len(vector["embedding"]) != DIM:
                    raise RuntimeError("Embedding 维度不是 512")
                embedded.append({**chunk, "embedding": vector["embedding"],
                                 "embedding_model": EMBED_MODEL, "embedding_dimension": DIM})
            call(c, "POST", "/vector-io/insert", {"vector_store_id": store_id, "chunks": embedded})
            state.update(job_id, done=min(start + 16, len(chunks)))
    state.update(job_id, status="ready", error=None)


def serve():
    state.init()
    with (DATA / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("已有 Worker 正在运行；这一版只允许一个 Worker")
        state.recover()
        child = None
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print("Worker ready (CPU, persistent queue)", flush=True)
        while not stopping:
            state.heartbeat()
            job = state.claim()
            if not job:
                time.sleep(1)
                continue
            job_id = job["id"]
            log_path = DATA / "artifacts" / (job_id + ".log")
            with log_path.open("ab") as log:
                child = subprocess.Popen([sys.executable, "-u", __file__, "--task", job_id],
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                         pass_fds=(lock.fileno(),))
                deadline = time.monotonic() + int(os.environ.get("RAG_TASK_TIMEOUT", "1800"))
                timed_out = False
                while child.poll() is None:
                    state.heartbeat()
                    if stopping or time.monotonic() > deadline:
                        timed_out = not stopping
                        os.killpg(child.pid, signal.SIGTERM)
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid, signal.SIGKILL)
                            child.wait()
                        break
                    time.sleep(1)
                if stopping:
                    if state.get(job_id)["status"] != "ready":
                        state.update(job_id, status="queued")
                elif state.get(job_id)["status"] != "ready":
                    with log_path.open("rb") as log_read:
                        log_read.seek(max(0, log_path.stat().st_size - 3000))
                        error = log_read.read().decode("utf-8", errors="replace")
                    state.update(job_id, status="failed",
                                 error=("任务超时。\n" if timed_out else "") + error)
            print(job_id, state.get(job_id)["status"], flush=True)
            child = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task")
    args = parser.parse_args()
    if args.task:
        process(args.task)
    else:
        serve()
