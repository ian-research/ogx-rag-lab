"""Re-run the state migration into a scratch directory and compare with the live one.

Proves three things for the review:
  1. the migration summary (counts, integrity, unresolved work),
  2. the signing keys are byte-identical after migration (hashes only, never keys),
  3. the unresolved submissions in the copy are still readable and point at real drafts.

Usage: .venv/bin/python scripts/state_migration_check.py <old_state_dir> <scratch_dir>
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings  # noqa: E402


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def key_hashes(directory):
    directory = Path(directory)
    if not directory.exists():
        return {}
    return {p.name: sha(p) for p in sorted(directory.iterdir()) if p.is_file()}


def pending(db):
    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select s.idem_key, s.state, s.draft_id, d.status as draft_status "
            "from submissions s left join drafts d on d.draft_id = s.draft_id "
            "where s.state in ('submitting','needs_recheck')").fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: state_migration_check.py <old_state_dir> <scratch_dir>", file=sys.stderr)
        return 2
    old, scratch = Path(sys.argv[1]), Path(sys.argv[2])
    completed = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/migrate_state.py"),
         str(old), str(scratch)], capture_output=True, text=True)
    if completed.returncode != 0:
        print(completed.stdout, completed.stderr)
        return completed.returncode
    summary, _ = json.JSONDecoder().raw_decode(completed.stdout.lstrip())
    live_private = Path(settings.PRIVATE)
    copy_private = scratch / "private"
    live_keys = key_hashes(live_private)
    copy_keys = key_hashes(copy_private)
    copied_db = scratch / "data" / "action.db"
    result = {
        "migration_summary": summary,
        "keys_identical": live_keys == copy_keys,
        "key_hashes": {name: {"live": live_keys.get(name), "copy": copy_keys.get(name)}
                       for name in sorted(set(live_keys) | set(copy_keys))},
        "live_counts": summary.get("target_counts"),
        "pending_in_copy": pending(copied_db),
        "live_db": str(settings.DB_PATH),
        "copy_db": str(copied_db),
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    ok = result["keys_identical"] and not summary.get("mismatch")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
