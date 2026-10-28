"""版本元数据存储（SQLite）。与第一阶段 jobs.sqlite 完全分离。"""
import os
import sqlite3
from pathlib import Path

DATA = Path(os.environ.get("VERSION_DATA_DIR", "/root/rag-lab/rag-version-lab/data"))
DB = DATA / "version.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS versions (
  version_id         TEXT PRIMARY KEY,
  policy_id          TEXT NOT NULL,
  policy_title       TEXT,
  version_label      TEXT NOT NULL,
  scope              TEXT NOT NULL,
  filename           TEXT,
  source_path        TEXT,
  sha256             TEXT,
  store_id           TEXT,
  job_status         TEXT NOT NULL DEFAULT 'queued',
  publication_status TEXT NOT NULL DEFAULT 'draft',
  valid_from         TEXT NOT NULL,
  valid_to           TEXT,
  supersedes         TEXT,
  published_at       TEXT,
  withdrawn_at       TEXT,
  error              TEXT,
  total              INTEGER DEFAULT 0,
  done               INTEGER DEFAULT 0,
  created_at         TEXT NOT NULL,
  UNIQUE(policy_id, scope, version_label)
);
CREATE TABLE IF NOT EXISTS publish_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  action     TEXT NOT NULL,
  version_id TEXT,
  policy_id  TEXT,
  scope      TEXT,
  detail     TEXT,
  at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_status (
  id INTEGER PRIMARY KEY CHECK (id=1), heartbeat REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ingestion_attempts (
  id INTEGER PRIMARY KEY, version_id TEXT NOT NULL, store_id TEXT,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_versions_lookup
  ON versions(policy_id, scope, publication_status, valid_from);
"""


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)


def get(conn, version_id):
    row = conn.execute("SELECT * FROM versions WHERE version_id=?", (version_id,)).fetchone()
    return dict(row) if row else None


def list_versions(conn, policy_id=None, scope=None):
    sql = "SELECT * FROM versions WHERE 1=1"
    args = []
    if policy_id:
        sql += " AND policy_id=?"; args.append(policy_id)
    if scope:
        sql += " AND scope=?"; args.append(scope)
    sql += " ORDER BY policy_id, scope, valid_from"
    return [dict(r) for r in conn.execute(sql, args)]


def write_log(conn, action, version=None, detail=None, at=None):
    import datetime
    conn.execute(
        "INSERT INTO publish_log(action, version_id, policy_id, scope, detail, at) "
        "VALUES (?,?,?,?,?,?)",
        (action, (version or {}).get("version_id"), (version or {}).get("policy_id"),
         (version or {}).get("scope"), detail,
         at or datetime.datetime.now().isoformat(timespec="seconds")),
    )
