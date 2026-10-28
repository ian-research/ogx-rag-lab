"""版本规则：判定与事务集中在此，便于单测。"""
import datetime
import re
import json
import version_store as store
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
OPEN_END = "9999-12-31"


class RuleError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def business_today():
    return datetime.datetime.now(BUSINESS_TZ).date().isoformat()


def normalize_date(value, field="date"):
    if value is None or value == "":
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("noncanonical date")
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise RuleError("bad_date", f"{field} 必须是 YYYY-MM-DD：{value!r}")


def in_range(version, as_of):
    """有效区间 [valid_from, valid_to)"""
    return version["valid_from"] <= as_of < (version["valid_to"] or OPEN_END)


def overlaps(a_from, a_to, b_from, b_to):
    return a_from < (b_to or OPEN_END) and b_from < (a_to or OPEN_END)


def check_publishable(conn, version):
    if version is None:
        raise RuleError("not_found", "版本不存在")
    if version["publication_status"] == "published":
        return False, "already_published"
    if version["job_status"] != "ready":
        return False, f"not_ready:{version['job_status']}"
    if not version["valid_from"]:
        return False, "missing_valid_from"
    if not version["scope"]:
        return False, "missing_scope"

    start = normalize_date(version["valid_from"], "valid_from")
    end = normalize_date(version["valid_to"], "valid_to")
    if end and end <= start:
        raise RuleError("bad_date", "valid_to 必须晚于 valid_from")
    supersedes = version["supersedes"]
    if supersedes:
        row = conn.execute("SELECT * FROM versions WHERE version_id=?", (supersedes,)).fetchone()
        if not row:
            raise RuleError("bad_supersedes", f"被替代版本不存在：{supersedes}")
        target = dict(row)
        if target["policy_id"] != version["policy_id"] or target["scope"] != version["scope"]:
            raise RuleError("bad_supersedes", "被替代版本必须同文档、同适用范围")
        if target["publication_status"] != "published":
            raise RuleError("conflict", "被替代版本不处于已发布状态")
        if target["valid_to"] and start > target["valid_to"]:
            raise RuleError("conflict", "不能延长旧版有效期；请选择实际前一版本")
        if version["valid_from"] <= target["valid_from"]:
            raise RuleError("bad_supersedes",
                            f"新版生效日期 {version['valid_from']} 必须晚于旧版 {target['valid_from']}")

    published = conn.execute(
        "SELECT * FROM versions WHERE policy_id=? AND scope=? AND publication_status='published'",
        (version["policy_id"], version["scope"]),
    ).fetchall()
    for other in published:
        other = dict(other)
        if other["version_id"] == (supersedes or ""):
            continue
        if overlaps(version["valid_from"], version["valid_to"],
                    other["valid_from"], other["valid_to"]):
            return False, f"overlap:{other['version_id']}"
    return True, "ok"


def publish(conn, version_id, now=None):
    now = store.timestamp_utc(now) if now else store.utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM versions WHERE version_id=?", (version_id,)).fetchone()
        version = dict(row) if row else None
        ok, reason = check_publishable(conn, version)
        if not ok:
            if reason == "already_published":
                conn.execute("ROLLBACK")
                return {"result": "idempotent", "version_id": version_id}
            raise RuleError("conflict", reason)

        if version["supersedes"]:
            previous = conn.execute("SELECT valid_to FROM versions WHERE version_id=?",
                                    (version["supersedes"],)).fetchone()[0]
            updated = conn.execute(
                "UPDATE versions SET valid_to=? WHERE version_id=? AND publication_status='published'",
                (version["valid_from"], version["supersedes"]),
            ).rowcount
            if updated != 1:
                raise RuleError("conflict", "被替代版本不处于已发布状态")
            conn.execute(
                "INSERT INTO publish_log(action, version_id, policy_id, scope, detail, at) "
                "VALUES ('supersede',?,?,?,?,?)",
                (version["supersedes"], version["policy_id"], version["scope"],
                 f"valid_to={version['valid_from']}", now),
            )

            log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO replacement_snapshots VALUES (?,?,?,?,?)",
                         (version_id, version["supersedes"], previous, version["valid_from"], log_id))

        conn.execute(
            "UPDATE versions SET publication_status='published', withdrawn_at=NULL, published_at=? WHERE version_id=?",
            (now, version_id),
        )
        conn.execute(
            "INSERT INTO publish_log(action, version_id, policy_id, scope, detail, at) "
            "VALUES ('publish',?,?,?,?,?)",
            (version_id, version["policy_id"], version["scope"],
             f"valid_from={version['valid_from']}", now),
        )
        conn.execute("COMMIT")
        return {"result": "published", "version_id": version_id}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def withdraw(conn, version_id, reason="manual", now=None):
    now = store.timestamp_utc(now) if now else store.utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM versions WHERE version_id=?", (version_id,)).fetchone()
        if not row:
            raise RuleError("not_found", "版本不存在")
        version = dict(row)
        if version["publication_status"] != "published":
            raise RuleError("conflict",
                            f"只有已发布版本可撤回，当前 {version['publication_status']}")
        conn.execute(
            "UPDATE versions SET publication_status='withdrawn', withdrawn_at=? WHERE version_id=?",
            (now, version_id),
        )
        conn.execute(
            "INSERT INTO publish_log(action, version_id, policy_id, scope, detail, at) "
            "VALUES ('withdraw',?,?,?,?,?)",
            (version_id, version["policy_id"], version["scope"], reason, now),
        )
        restoration = restore_predecessor(conn, version, now)
        conn.execute("COMMIT")
        return {"result": "withdrawn", "version_id": version_id, "restoration": restoration}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def restore_predecessor(conn, version, now):
    snapshot = conn.execute("SELECT * FROM replacement_snapshots WHERE version_id=?",
                            (version["version_id"],)).fetchone()
    if not snapshot:
        return {"status": "skipped", "reason": "legacy_snapshot_missing" if version["supersedes"] else "no_predecessor"}
    snap = dict(snapshot)
    target = conn.execute("SELECT * FROM versions WHERE version_id=?", (snap["target_id"],)).fetchone()
    reason = None
    if not target or target["publication_status"] != "published":
        reason = "predecessor_not_published"
    elif target["valid_to"] != snap["cutoff"]:
        reason = "predecessor_changed"
    elif conn.execute("SELECT 1 FROM publish_log WHERE version_id=? AND id>? AND action IN ('supersede','restore','publish','withdraw')",
                      (snap["target_id"], snap["supersede_log_id"])).fetchone():
        reason = "predecessor_changed"
    else:
        others = conn.execute("SELECT * FROM versions WHERE policy_id=? AND scope=? AND publication_status='published' AND version_id!=?",
                              (target["policy_id"], target["scope"], snap["target_id"]))
        if any(overlaps(target["valid_from"], snap["previous_valid_to"], r["valid_from"], r["valid_to"]) for r in others):
            reason = "overlap"
    if reason:
        store.write_log(conn, 'restore_skipped', version, reason, now)
        return {"status": "skipped", "reason": reason}
    conn.execute("UPDATE versions SET valid_to=? WHERE version_id=?", (snap["previous_valid_to"], snap["target_id"]))
    store.write_log(conn, 'restore', dict(target), json.dumps({"withdrawn_version": version["version_id"], "valid_to": snap["previous_valid_to"]}), now)
    return {"status": "restored", "version_id": snap["target_id"], "valid_to": snap["previous_valid_to"]}


def resolve(conn, policy_id=None, scope=None, as_of=None):
    """选出 as_of 当天有效的已发布版本。"""
    if not scope:
        return {"status": "missing_scope", "versions": [], "as_of": as_of}
    as_of = normalize_date(as_of or business_today(), "as_of")
    sql = "SELECT * FROM versions WHERE publication_status='published' AND scope=?"
    args = [scope]
    if policy_id:
        sql += " AND policy_id=?"; args.append(policy_id)
    rows = [dict(r) for r in conn.execute(sql, args)]
    hits = [r for r in rows if in_range(r, as_of)]
    if not hits:
        return {"status": "no_data", "versions": [], "as_of": as_of}
    if len({r["policy_id"] for r in hits}) > 1:
        return {"status": "needs_selection", "versions": hits, "as_of": as_of,
                "reason": "存在多个适用文档，请指定 policy_id；这不代表正文存在矛盾"}
    if len(hits) > 1:
        return {"status": "conflict", "versions": hits, "as_of": as_of,
                "reason": "同一文档下存在多个重叠的有效版本"}
    return {"status": "ok", "versions": hits, "as_of": as_of}
