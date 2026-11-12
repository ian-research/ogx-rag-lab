"""Move the persistent state to a new directory without losing pending work.

Uses sqlite3's online backup API, so WAL contents are included; the copy is then
verified (integrity_check + row counts + unresolved submissions) before the old
directory is left untouched for rollback.
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path

TABLES = ("drafts", "confirmations", "submissions", "tickets", "audit", "seq")


def counts(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        conn.close()


def unresolved(db: Path) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT count(*) FROM submissions WHERE state IN ('submitting','needs_recheck')"
        ).fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: migrate_state.py <old_state_dir> <new_state_dir>", file=sys.stderr)
        return 2
    old, new = Path(sys.argv[1]), Path(sys.argv[2])
    src_db = old / "data" / "action.db"
    if not src_db.exists():
        print(f"no database at {src_db}; nothing to migrate")
        return 1
    (new / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
    (new / "private").mkdir(parents=True, exist_ok=True, mode=0o700)
    (new / "evidence" / "runs").mkdir(parents=True, exist_ok=True, mode=0o700)
    dst_db = new / "data" / "action.db"
    if dst_db.exists():
        print(f"refusing to overwrite existing database at {dst_db}", file=sys.stderr)
        return 1

    source = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    target = sqlite3.connect(dst_db)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    before, after = counts(src_db), counts(dst_db)
    check = sqlite3.connect(f"file:{dst_db}?mode=ro", uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()

    keys = ("sessions.json", "action.key", "credentials-for-delivery.json")
    copied = []
    for name in keys:
        src = old / "private" / name
        if src.exists():
            shutil.copy2(src, new / "private" / name)
            (new / "private" / name).chmod(0o600)
            copied.append(name)

    report = {"integrity_check": integrity, "source_counts": before, "target_counts": after,
              "pending_submissions": unresolved(dst_db), "keys_copied": copied,
              "old_state_dir": str(old), "new_state_dir": str(new)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = integrity == "ok" and before == after and len(copied) == 3
    print("migrate: ok" if ok else "migrate: FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
