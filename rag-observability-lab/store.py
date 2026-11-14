"""SQLite persistence for drafts, confirmations, submissions and simulated tickets.

All write paths run in explicit transactions; the ticket insert is guarded by the
primary key on the idempotency key, so a replay can never create a second record.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts(
  draft_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, version INTEGER NOT NULL,
  asset_id TEXT, asset_model TEXT, purchase_date TEXT, reason TEXT NOT NULL,
  facts_json TEXT NOT NULL, basis_json TEXT NOT NULL, missing_json TEXT NOT NULL,
  content_hash TEXT NOT NULL, canonical_json TEXT, status TEXT NOT NULL,
  idem_key TEXT, locked_at TEXT, reason_source TEXT, trace_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS confirmations(
  confirmation_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, user_id TEXT NOT NULL,
  version INTEGER NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL,
  issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT, trace_id TEXT);
CREATE TABLE IF NOT EXISTS submissions(
  idem_key TEXT PRIMARY KEY, user_id TEXT NOT NULL, draft_id TEXT NOT NULL,
  version INTEGER NOT NULL, content_hash TEXT NOT NULL, confirmation_id TEXT NOT NULL,
  state TEXT NOT NULL, ticket_no TEXT, last_error TEXT, request_json TEXT,
  authorization_json TEXT, lease_owner TEXT, lease_until TEXT, trace_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets(
  ticket_no TEXT PRIMARY KEY, idem_key TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL,
  asset_id TEXT, reason TEXT NOT NULL, content_hash TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT,
  action TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS seq(day TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _later(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(
        microsecond=0).isoformat()


@contextmanager
def connect():
    settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def _migrate(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(drafts)")}
    if "idem_key" not in cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN idem_key TEXT")
    if "locked_at" not in cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN locked_at TEXT")
    if "canonical_json" not in cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN canonical_json TEXT")
    if "reason_source" not in cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN reason_source TEXT")
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(submissions)")}
    if "request_json" not in cols:
        conn.execute("ALTER TABLE submissions ADD COLUMN request_json TEXT")
    if "authorization_json" not in cols:
        conn.execute("ALTER TABLE submissions ADD COLUMN authorization_json TEXT")
    if "lease_owner" not in cols:
        conn.execute("ALTER TABLE submissions ADD COLUMN lease_owner TEXT")
    if "lease_until" not in cols:
        conn.execute("ALTER TABLE submissions ADD COLUMN lease_until TEXT")
    if "trace_id" not in cols:
        conn.execute("ALTER TABLE submissions ADD COLUMN trace_id TEXT")
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(drafts)")}
    if "trace_id" not in cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN trace_id TEXT")
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(confirmations)")}
    if "trace_id" not in cols:
        conn.execute("ALTER TABLE confirmations ADD COLUMN trace_id TEXT")


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def audit(actor, action, detail=None):
    with connect() as conn:
        conn.execute("INSERT INTO audit(at, actor, action, detail) VALUES(?,?,?,?)",
                     (utcnow(), actor, action, json.dumps(detail or {}, ensure_ascii=False)))


def create_draft(built):
    now = utcnow()
    with connect() as conn:
        conn.execute(
            "INSERT INTO drafts(draft_id,user_id,version,asset_id,asset_model,purchase_date,reason,"
            "reason_source,facts_json,basis_json,missing_json,content_hash,canonical_json,status,"
            "created_at,updated_at,expires_at,trace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (built["draft_id"], built["user_id"], built["version"], built["asset_id"],
             built["asset_model"], built["purchase_date"], built["reason"],
             built.get("reason_source") or "template",
             json.dumps(built["facts"], ensure_ascii=False),
             json.dumps(built["basis"], ensure_ascii=False),
             json.dumps(built["missing"], ensure_ascii=False),
             built["content_hash"], built["canonical_json"], built["status"], now, now,
             _later(settings.DRAFT_TTL_SECONDS), built.get("trace_id")))
    return get_draft(built["draft_id"])


def get_draft(draft_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()


def revise_draft(draft_id, user_id, reason, new_hash, canonical_json):
    """Only an unlocked draft may be edited.

    Once a submission has been opened the content is frozen: the identifier and the
    content hash are what the back office already keyed on.
    """
    now = utcnow()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM drafts WHERE draft_id=? AND user_id=?",
                           (draft_id, user_id)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None, "not_found"
        if row["status"] != "awaiting_confirmation":
            conn.execute("ROLLBACK")
            return None, row["status"]
        conn.execute("UPDATE drafts SET version=version+1, reason=?, reason_source=?, content_hash=?, "
                     "canonical_json=?, status='awaiting_confirmation', idem_key=NULL, "
                     "locked_at=NULL, updated_at=?, expires_at=? WHERE draft_id=?",
                     (reason, "user", new_hash, canonical_json, now,
                      _later(settings.DRAFT_TTL_SECONDS), draft_id))
        conn.execute("UPDATE confirmations SET status='superseded' "
                     "WHERE draft_id=? AND status='active'", (draft_id,))
        conn.execute("COMMIT")
    return get_draft(draft_id), None


def cancel_draft(draft_id, user_id):
    """Cancelling is only meaningful before a submission is opened."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM drafts WHERE draft_id=? AND user_id=?",
                           (draft_id, user_id)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None, "not_found"
        if row["status"] != "awaiting_confirmation":
            conn.execute("ROLLBACK")
            return get_draft(draft_id), row["status"]
        conn.execute("UPDATE drafts SET status='cancelled', updated_at=? WHERE draft_id=?",
                     (utcnow(), draft_id))
        conn.execute("UPDATE confirmations SET status='cancelled' WHERE draft_id=? AND status='active'",
                     (draft_id,))
        conn.execute("COMMIT")
    return get_draft(draft_id), None


def lock_draft_for_submit(draft_id, user_id, idem_key):
    """Claim the draft for a submission and freeze its content.

    Returns (row, error). The row is the locked draft; error is one of
    not_found / already_locked / wrong_state.
    """
    now = utcnow()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM drafts WHERE draft_id=? AND user_id=?",
                           (draft_id, user_id)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None, "not_found"
        if row["status"] == "awaiting_confirmation":
            if row["idem_key"] and row["idem_key"] != idem_key:
                conn.execute("ROLLBACK")
                return None, "key_changed"
            conn.execute("UPDATE drafts SET status='submitting', idem_key=?, locked_at=?, "
                         "updated_at=? WHERE draft_id=?", (idem_key, now, now, draft_id))
            conn.execute("COMMIT")
            return get_draft(draft_id), None
        if row["status"] in ("submitting", "submitted_unknown", "submitted"):
            if row["idem_key"] and row["idem_key"] != idem_key:
                conn.execute("ROLLBACK")
                return None, "key_changed"
            conn.execute("COMMIT")
            return get_draft(draft_id), row["status"]
        conn.execute("ROLLBACK")
        return None, row["status"]


def release_draft(draft_id, status, clear_key=False):
    """Release a frozen draft.

    A definite refusal (nothing was written) clears the identifier so the user can
    edit and submit again; an unknown outcome keeps it frozen because the content
    must stay the one the back office may already hold.
    """
    with connect() as conn:
        if clear_key:
            conn.execute("UPDATE drafts SET status=?, idem_key=NULL, locked_at=NULL, "
                         "updated_at=? WHERE draft_id=?", (status, utcnow(), draft_id))
        else:
            conn.execute("UPDATE drafts SET status=?, updated_at=? WHERE draft_id=?",
                         (status, utcnow(), draft_id))


def set_draft_status(draft_id, status, actor=None):
    with connect() as conn:
        conn.execute("UPDATE drafts SET status=?, updated_at=? WHERE draft_id=?",
                     (status, utcnow(), draft_id))
    if actor:
        audit(actor, "draft_" + status, {"draft_id": draft_id})


def create_confirmation(draft_row, confirmation_id, trace_id=None):
    now = utcnow()
    expires = _later(settings.CONFIRM_TTL_SECONDS)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE confirmations SET status='superseded' "
                     "WHERE draft_id=? AND status='active'", (draft_row["draft_id"],))
        conn.execute("INSERT INTO confirmations(confirmation_id,draft_id,user_id,version,"
                     "content_hash,status,issued_at,expires_at,trace_id) "
                     "VALUES(?,?,?,?,?,'active',?,?,?)",
                     (confirmation_id, draft_row["draft_id"], draft_row["user_id"],
                      draft_row["version"], draft_row["content_hash"], now, expires, trace_id))
        conn.execute("COMMIT")
    return get_confirmation(confirmation_id)


def get_confirmation(confirmation_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM confirmations WHERE confirmation_id=?",
                            (confirmation_id,)).fetchone()


def open_submission(idem_key, user_id, draft_id, version, content_hash, confirmation_id):
    now = utcnow()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO submissions(idem_key,user_id,draft_id,version,content_hash,"
            "confirmation_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,'submitting',?,?)",
            (idem_key, user_id, draft_id, version, content_hash, confirmation_id, now, now))
        created = cursor.rowcount == 1
        conn.execute("COMMIT")
    return created, get_submission(idem_key)


def authorize_and_lock(draft_id, user_id, idem_key, confirmation_id, trace_id=None):
    """Validate the confirmation, freeze the draft and open the submission record.

    All three happen in one transaction: the lock alone is never evidence that a
    confirmation was checked, and a submission without an authorization record can
    never be replayed into a ticket.

    Returns (draft_row, submission_row, error) where error is one of
    not_found / wrong_state / key_changed / orphan_lock / confirmation_* .
    """
    now = utcnow()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM drafts WHERE draft_id=? AND user_id=?",
                           (draft_id, user_id)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None, None, "not_found"

        existing = conn.execute("SELECT * FROM submissions WHERE idem_key=?",
                                (idem_key,)).fetchone()

        if row["status"] == "awaiting_confirmation":
            if row["idem_key"] and row["idem_key"] != idem_key:
                conn.execute("ROLLBACK")
                return None, None, "key_changed"
            confirmation = conn.execute(
                "SELECT * FROM confirmations WHERE confirmation_id=?",
                (confirmation_id or "",)).fetchone()
            if confirmation is None or confirmation["user_id"] != user_id:
                conn.execute("ROLLBACK")
                return None, None, "confirmation_missing"
            if confirmation["draft_id"] != draft_id:
                conn.execute("ROLLBACK")
                return None, None, "confirmation_other_draft"
            if confirmation["status"] != "active":
                conn.execute("ROLLBACK")
                return None, None, "confirmation_" + confirmation["status"]
            if confirmation["expires_at"] <= now:
                conn.execute("ROLLBACK")
                return None, None, "confirmation_expired"
            if (confirmation["version"] != row["version"]
                    or confirmation["content_hash"] != row["content_hash"]):
                conn.execute("ROLLBACK")
                return None, None, "confirmation_stale"
            authorization = {"confirmation_id": confirmation_id,
                             "user_id": user_id, "draft_id": draft_id,
                             "version": row["version"],
                             "content_hash": row["content_hash"],
                             "authorized_at": now}
            conn.execute(
                "INSERT OR IGNORE INTO submissions(idem_key,user_id,draft_id,version,"
                "content_hash,confirmation_id,state,authorization_json,lease_until,"
                "created_at,updated_at,trace_id) VALUES(?,?,?,?,?,?,'submitting',?,?,?,?,?)",
                (idem_key, user_id, draft_id, row["version"], row["content_hash"],
                 confirmation_id, json.dumps(authorization, sort_keys=True),
                 _lease_deadline(now), now, now, trace_id))
            conn.execute("UPDATE drafts SET status='submitting', idem_key=?, locked_at=?, "
                         "updated_at=? WHERE draft_id=?", (idem_key, now, now, draft_id))
            conn.execute("COMMIT")
            return get_draft(draft_id), get_submission(idem_key), None

        if row["status"] in ("submitting", "submitted_unknown", "submitted"):
            if existing is None or not existing["authorization_json"]:
                conn.execute("ROLLBACK")
                return get_draft(draft_id), existing, "orphan_lock"
            if row["idem_key"] and row["idem_key"] != idem_key:
                conn.execute("ROLLBACK")
                return None, None, "key_changed"
            conn.execute("COMMIT")
            return get_draft(draft_id), existing, None

        conn.execute("ROLLBACK")
        return None, None, row["status"]


LEASE_SECONDS = 120


def _lease_deadline(start: str | None = None) -> str:
    base = datetime.fromisoformat(start or utcnow())
    return (base + timedelta(seconds=LEASE_SECONDS)).replace(microsecond=0).isoformat()


def lease_expired(row) -> bool:
    until = row["lease_until"] if "lease_until" in row.keys() else None
    return not until or until <= utcnow()


def take_lease(idem_key, owner):
    """CAS takeover of a submitting record whose lease ran out.

    Only one caller can win: the update is conditional on the previous deadline
    being in the past, so a live request keeps its lease.
    """
    now = utcnow()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE submissions SET lease_owner=?, lease_until=?, state='submitting', updated_at=? "
            "WHERE idem_key=? AND state IN ('submitting','needs_recheck') "
            "AND (lease_until IS NULL OR lease_until<?)",
            (owner, _lease_deadline(now), now, idem_key, now))
        return cur.rowcount == 1


def renew_lease(idem_key, owner):
    with connect() as conn:
        cur = conn.execute(
            "UPDATE submissions SET lease_until=?, updated_at=? "
            "WHERE idem_key=? AND state='submitting' AND lease_owner=?",
            (_lease_deadline(), utcnow(), idem_key, owner))
        return cur.rowcount == 1


def mark_orphan_recovered(draft_id, status):
    """Explicitly recover a draft whose lock has no authorization record."""
    with connect() as conn:
        conn.execute("UPDATE drafts SET status=?, idem_key=NULL, locked_at=NULL, "
                     "updated_at=? WHERE draft_id=?", (status, utcnow(), draft_id))


def get_submission(idem_key):
    with connect() as conn:
        return conn.execute("SELECT * FROM submissions WHERE idem_key=?", (idem_key,)).fetchone()


def set_submission_request(idem_key, request):
    with connect() as conn:
        conn.execute("UPDATE submissions SET request_json=?, updated_at=? WHERE idem_key=?",
                     (json.dumps(request, ensure_ascii=False, sort_keys=True), utcnow(), idem_key))


def update_submission(idem_key, state, ticket_no=None, error=None):
    with connect() as conn:
        conn.execute("UPDATE submissions SET state=?, ticket_no=COALESCE(?, ticket_no), "
                     "last_error=?, updated_at=? WHERE idem_key=?",
                     (state, ticket_no, error, utcnow(), idem_key))


def list_submissions(user_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM submissions WHERE user_id=? ORDER BY created_at DESC",
                            (user_id,)).fetchall()


def _next_number(conn, day):
    row = conn.execute("INSERT INTO seq(day, n) VALUES(?, 1) "
                       "ON CONFLICT(day) DO UPDATE SET n=n+1 RETURNING n", (day,)).fetchone()
    return row[0]


def insert_ticket(idem_key, user_id, asset_id, reason, content_hash, payload):
    now = utcnow()
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM tickets WHERE idem_key=?", (idem_key,)).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            outcome = "duplicate" if existing["content_hash"] == content_hash else "conflict"
            return outcome, existing
        number = _next_number(conn, day)
        ticket_no = f"RT-{day}-{number:04d}"
        conn.execute("INSERT INTO tickets(ticket_no,idem_key,user_id,asset_id,reason,content_hash,"
                     "status,created_at,payload_json) VALUES(?,?,?,?,?,?,'pending_review',?,?)",
                     (ticket_no, idem_key, user_id, asset_id, reason, content_hash, now,
                      json.dumps(payload, ensure_ascii=False)))
        conn.execute("COMMIT")
    return "created", get_ticket(ticket_no)


def ticket_by_key(idem_key):
    with connect() as conn:
        return conn.execute("SELECT * FROM tickets WHERE idem_key=?", (idem_key,)).fetchone()


def get_ticket(ticket_no):
    with connect() as conn:
        return conn.execute("SELECT * FROM tickets WHERE ticket_no=?", (ticket_no,)).fetchone()


def user_tickets(user_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY created_at DESC",
                            (user_id,)).fetchall()


def get_trace_links(draft_id):
    """Join the three requests that belong to one application, by stored trace ids.

    The identifiers are written when each request happens; this function only reads,
    so a broken trace backend cannot change what the business state says.
    """
    with connect() as conn:
        draft = conn.execute("SELECT draft_id, version, status, trace_id FROM drafts "
                             "WHERE draft_id=?", (draft_id,)).fetchone()
        if draft is None:
            return None
        confirmation = conn.execute(
            "SELECT confirmation_id, version, status, trace_id FROM confirmations "
            "WHERE draft_id=? ORDER BY issued_at DESC LIMIT 1", (draft_id,)).fetchone()
        submission = conn.execute(
            "SELECT idem_key, state, ticket_no, trace_id FROM submissions "
            "WHERE draft_id=? ORDER BY created_at DESC LIMIT 1", (draft_id,)).fetchone()
    return {"draft": dict(draft),
            "confirmation": dict(confirmation) if confirmation else None,
            "submission": dict(submission) if submission else None}
