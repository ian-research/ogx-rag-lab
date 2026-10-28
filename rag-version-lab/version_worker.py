"""后台入库：按队列处理未就绪版本。复用第一阶段解析/分块/向量写入。"""
import argparse
import fcntl
import signal
import subprocess
import json
import os
import sys
import time
import traceback
from pathlib import Path

# 第一阶段模块目录。用 append 而不是 insert(0)：
# 脚本自身目录（sys.path[0]）优先，测试夹具里的同名模块才不会被真实模块遮蔽。
# 需要指向别处时用 RAG_PHASE1_DIR 覆盖，便于隔离测试。
PHASE1 = Path(os.environ.get("RAG_PHASE1_DIR", "/root/rag-lab/rag-upload-lab"))
if str(PHASE1) not in sys.path:
    sys.path.append(str(PHASE1))

from ogx_client import client, call   # noqa: E402
from settings import EMBED_MODEL, DIM, MAX_TOKENS, TOKENIZER   # noqa: E402

import version_store as store   # noqa: E402

DATA = store.DATA
ARTIFACTS = DATA / "artifacts"
WORK = DATA / "work"
POLL = float(os.environ.get("VERSION_WORKER_POLL", "3"))

_tokenizer = None


def tokenizer():
    global _tokenizer
    if _tokenizer is None:
        from transformers import AutoTokenizer
        _tokenizer = AutoTokenizer.from_pretrained(TOKENIZER, local_files_only=True)
    return _tokenizer


def claim(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM versions WHERE job_status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        conn.execute("UPDATE versions SET job_status='parsing', error=NULL WHERE version_id=?",
                     (row["version_id"],))
        conn.execute("COMMIT")
        return dict(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise


def update(conn, version_id, **fields):
    allowed = {"job_status", "store_id", "total", "done", "error"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"不允许更新字段 {bad}")
    keys = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE versions SET {keys} WHERE version_id=?",
                 (*fields.values(), version_id))
    conn.commit()


# 冻结片段复用前必须满足的结构。chunk_metadata 是 OGX /vector-io/insert 的必需字段，
# 缺失会被远端直接 400，因此必须在本进程内先拦下，不能依赖远端报错。
CHUNK_REQUIRED_FIELDS = ("chunk_id", "content", "chunk_metadata")


class ChunkValidationError(RuntimeError):
    """冻结片段结构不合法。不修补、不填充默认值，直接判失败。"""


def validate_chunks(chunks):
    """校验冻结片段。通过返回原列表，不合法抛 ChunkValidationError。

    只做校验与类型检查，不改写内容、不生成字段、不改动 chunk_id。
    """
    if not isinstance(chunks, list):
        raise ChunkValidationError(
            f"冻结片段必须是列表，实际为 {type(chunks).__name__}")
    if not chunks:
        raise ChunkValidationError("冻结片段是空列表")
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise ChunkValidationError(
                f"第 {index} 项必须是对象，实际为 {type(chunk).__name__}")
        for field in CHUNK_REQUIRED_FIELDS:
            if field not in chunk:
                raise ChunkValidationError(f"第 {index} 项缺少必需字段 {field}")
        if not isinstance(chunk["chunk_id"], str) or not chunk["chunk_id"].strip():
            raise ChunkValidationError(f"第 {index} 项 chunk_id 必须是非空字符串")
        if not isinstance(chunk["content"], str) or not chunk["content"].strip():
            raise ChunkValidationError(f"第 {index} 项 content 必须是非空字符串")
        if not isinstance(chunk["chunk_metadata"], dict):
            raise ChunkValidationError(
                f"第 {index} 项 chunk_metadata 必须是对象，实际为 {type(chunk['chunk_metadata']).__name__}")
    return chunks


def load_frozen_chunks(work_file):
    """读取并校验冻结片段。任何解析或结构问题都在这里抛出。"""
    try:
        raw = work_file.read_text(encoding="utf-8")
    except OSError as error:
        raise ChunkValidationError(f"冻结片段不可读：{error}") from error
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ChunkValidationError(f"冻结片段 JSON 损坏：{error}") from error
    return validate_chunks(parsed)


def process(conn, version):
    vid = version["version_id"]
    src = Path(version["source_path"])
    log_path = ARTIFACTS / f"{vid}.log"
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    try:
        work_file = WORK / f"{vid}.chunks.json"
        WORK.mkdir(parents=True, exist_ok=True)
        if work_file.exists():
            # 校验发生在建向量库与调用 Embedding 之前；非法缓存直接失败。
            chunks = load_frozen_chunks(work_file)
        else:
            import parsing
            import chunking
            blocks = parsing.parse(str(src))
            if not blocks:
                raise RuntimeError("没有解析出内容")
            chunks = chunking.make_chunks(blocks, tokenizer(), vid, version["filename"], version["sha256"])
            if not chunks:
                raise RuntimeError("没有可入库片段")
            validate_chunks(chunks)
            temporary = work_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            temporary.replace(work_file)
        update(conn, vid, job_status="embedding", total=len(chunks), done=0)
        with client() as c:
            # Each attempt gets an isolated store: partial prior writes never mix with this attempt.
            attempt = conn.execute("INSERT INTO ingestion_attempts(version_id) VALUES (?)", (vid,)).lastrowid
            conn.commit()
            created = call(c, "POST", "/vector_stores", {
                "name": f"version-{vid}-attempt-{attempt}", "provider_id": "local-pgvector",
                "embedding_model": EMBED_MODEL, "embedding_dimension": DIM,
            })
            store_id = created["id"]
            conn.execute("UPDATE ingestion_attempts SET store_id=? WHERE id=?", (store_id, attempt))
            conn.commit()
            update(conn, vid, store_id=store_id)
            for start in range(0, len(chunks), 16):
                batch = chunks[start:start + 16]
                result = call(c, "POST", "/embeddings", {
                    "model": EMBED_MODEL,
                    "input": [chunk["content"] for chunk in batch],
                    "encoding_format": "float",
                })
                vectors = sorted(result["data"], key=lambda item: item["index"])
                if [v["index"] for v in vectors] != list(range(len(batch))):
                    raise RuntimeError("Embedding 返回数量或索引不匹配")
                embedded = []
                for chunk, vector in zip(batch, vectors):
                    if len(vector["embedding"]) != DIM:
                        raise RuntimeError("Embedding 维度不是 512")
                    embedded.append({**chunk, "embedding": vector["embedding"],
                                     "embedding_model": EMBED_MODEL,
                                     "embedding_dimension": DIM})
                call(c, "POST", "/vector-io/insert",
                     {"vector_store_id": store_id, "chunks": embedded})
                update(conn, vid, done=min(start + 16, len(chunks)))
        update(conn, vid, job_status="ready", error=None)
        log_path.write_text(f"ready chunks={len(chunks)}\n", encoding="utf-8")
    except Exception as error:
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        # 失败不改变任何已发布版本；仅把本版本标记 failed
        update(conn, vid, job_status="failed", error=str(error)[:500])


ACTIVE = ('parsing', 'chunking', 'embedding')


def heartbeat(conn):
    conn.execute("INSERT INTO worker_status(id,heartbeat) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat", (time.time(),))
    conn.commit()


def recover(conn):
    # Called only after acquiring the single-worker lock (also inherited by children).
    conn.execute("UPDATE versions SET job_status='queued',error='上次任务中断，重新入库' WHERE job_status IN ('parsing','chunking','embedding') AND publication_status='draft'")
    conn.commit()


def serve():
    store.init()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with (DATA / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('已有 Worker 运行')
        with store.connect() as conn:
            recover(conn)
            stopping = False

            def stop(*_):
                nonlocal stopping
                stopping = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            while not stopping:
                heartbeat(conn)
                version = claim(conn)
                if not version:
                    time.sleep(1)
                    continue
                vid = version['version_id']
                with (ARTIFACTS / f'{vid}.worker.log').open('ab') as log:
                    child = subprocess.Popen([sys.executable, '-u', __file__, '--task', vid],
                                             stdout=log, stderr=subprocess.STDOUT,
                                             start_new_session=True, pass_fds=(lock.fileno(),))
                    deadline = time.monotonic() + int(os.environ.get('VERSION_TASK_TIMEOUT', '3600'))
                    timed_out = False
                    while child.poll() is None:
                        heartbeat(conn)
                        if stopping or time.monotonic() >= deadline:
                            timed_out = not stopping
                            try:
                                os.killpg(child.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                            try:
                                child.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                os.killpg(child.pid, signal.SIGKILL)
                                child.wait()
                            break
                        time.sleep(1)
                    current = store.get(conn, vid)
                    if current['job_status'] in ACTIVE:
                        update(conn, vid, job_status='queued' if stopping else 'failed',
                               error='任务中断，可恢复' if stopping else '任务超时' if timed_out else '任务子进程异常退出，请查看日志')
            conn.execute('DELETE FROM worker_status WHERE id=1')
            conn.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--task')
    args = parser.parse_args()
    if args.task:
        with store.connect() as conn:
            version = store.get(conn, args.task)
            if not version or version['job_status'] not in ACTIVE or version['publication_status'] != 'draft':
                raise SystemExit('任务未被领取或不是草稿')
            process(conn, version)
    else:
        serve()
