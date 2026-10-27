"""本机持久队列；仅允许一个 Worker，用文件锁约束。"""
import sqlite3
import time
from contextlib import contextmanager

from settings import DATA, ACTIVE


@contextmanager
def db():
    connection = sqlite3.connect(DATA / "jobs.sqlite", timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "uploads").mkdir(exist_ok=True)
    (DATA / "artifacts").mkdir(exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
            path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            created REAL NOT NULL, updated REAL NOT NULL,
            store_id TEXT, total INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0, error TEXT, attempt INTEGER NOT NULL DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS worker_status (
            id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL NOT NULL)""")


def get(job_id):
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def update(job_id, **fields):
    allowed = {"status", "store_id", "total", "done", "error", "attempt"}
    if not fields.keys() <= allowed:
        raise ValueError("Invalid job field")
    fields["updated"] = time.time()
    with db() as c:
        c.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                  [*fields.values(), job_id])


def claim():
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:
            return None
        c.execute("UPDATE jobs SET status='parsing',updated=?,attempt=attempt+1,error=NULL WHERE id=?",
                  (time.time(), row["id"]))
        return dict(row)


def recover():
    # 调用者必须先持有 Worker 的进程锁；中断任务保留 store_id 以便幂等重试。
    with db() as c:
        c.execute("UPDATE jobs SET status='queued',updated=? WHERE status IN (?,?,?)",
                  (time.time(), *ACTIVE))


def heartbeat():
    with db() as c:
        c.execute("INSERT INTO worker_status VALUES (1,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat",
                  (time.time(),))
