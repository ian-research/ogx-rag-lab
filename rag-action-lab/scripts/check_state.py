"""Read-only integrity check for the shared state directory."""
import sqlite3
import sys
from pathlib import Path

TABLES = ("drafts", "confirmations", "submissions", "tickets", "audit")


def main() -> int:
    state = Path(sys.argv[1])
    db = state / "data" / "action.db"
    if not db.exists():
        print(f"state: no database at {db}")
        return 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"integrity_check: {ok}")
        counts = {}
        for table in TABLES:
            counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print("counts:", counts)
        wal = db.with_suffix(".db-wal")
        print("wal_present:", wal.exists())
        if ok != "ok":
            return 1
        sessions = state / "private" / "sessions.json"
        print("sessions_file:", sessions.exists())
        if not sessions.exists():
            return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
