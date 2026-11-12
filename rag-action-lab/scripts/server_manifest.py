"""Collect what is actually running on the lab host, for independent checking."""
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import security  # noqa: E402
import settings  # noqa: E402


def run(command):
    return subprocess.run(command, shell=True, capture_output=True, text=True).stdout.strip()


def sha(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _counts(path):
    import sqlite3
    if not path.exists():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: con.execute(f"select count(*) from {t}").fetchone()[0]
                for t in ("drafts", "confirmations", "submissions", "tickets", "audit")}
    finally:
        con.close()


def _verify_source(manifest):
    """Re-hash every deployed file and compare with the build manifest."""
    bad = []
    for name, expected in manifest["files_sha256"].items():
        actual = sha(ROOT / name)
        if actual != expected:
            bad.append({"file": name, "expected": expected, "actual": actual})
    return {"checked": len(manifest["files_sha256"]), "mismatch": bad,
            "matches_manifest": not bad}


def _process(unit):
    pid = run(f"systemctl show -p MainPID --value {unit}")
    cwd = run(f"readlink /proc/{pid}/cwd") if pid and pid != "0" else None
    exe = run(f"readlink /proc/{pid}/exe") if pid and pid != "0" else None
    return {"main_pid": pid, "cwd": cwd, "exe": exe}


def _state():
    private = Path(settings.PRIVATE)
    keys = {}
    if private.exists():
        for path in sorted(private.glob("*")):
            if path.is_file():
                keys[path.name] = {"sha256": sha(path),
                                   "mode": oct(path.stat().st_mode & 0o777)}
    pending = 0
    if settings.DB_PATH.exists():
        import sqlite3
        con = sqlite3.connect(f"file:{settings.DB_PATH}?mode=ro", uri=True)
        try:
            pending = con.execute(
                "select count(*) from submissions where state in ('submitting','needs_recheck')"
            ).fetchone()[0]
        finally:
            con.close()
    return {"dir": str(settings.STATE_DIR), "private": str(private),
            "files": keys, "pending_submissions": pending}


def main():
    units = ["article05-action-api", "article05-action-mcp"]
    manifest = json.loads((ROOT / "BUILD-MANIFEST.json").read_text())
    evidence = sorted(p.name for p in (ROOT / "evidence").iterdir() if p.is_dir())
    payload = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "host": {"hostname": platform.node(), "kernel": platform.release(),
                 "os": run("cat /etc/redhat-release")},
        "package": {"app_dir": str(ROOT), "file_count": manifest["file_count"],
                    "manifest_sha256": sha(ROOT / "BUILD-MANIFEST.json"),
                    "source": _verify_source(manifest)},
        "processes": {u: _process(u) for u in units},
        "state": _state(),
        "units": {u: {"sha256": sha(f"/etc/systemd/system/{u}.service"),
                      "active": run(f"systemctl is-active {u}"),
                      "enabled": run(f"systemctl is-enabled {u}")} for u in units},
        "config": {"path": "/etc/rag-lab/article05-runtime.env",
                   "sha256": sha("/etc/rag-lab/article05-runtime.env"),
                   "mode": oct((Path("/etc/rag-lab/article05-runtime.env").stat().st_mode & 0o777)
                               if Path("/etc/rag-lab/article05-runtime.env").exists() else 0),
                   "selinux": run("ls -Z /etc/rag-lab/article05-runtime.env | cut -d' ' -f1")},
        "ports": run("ss -ltn | grep -E ':8092|:8093'"),
        "health": {"api": run("curl -s http://127.0.0.1:8092/api/health"),
                   "mcp": run("curl -s http://127.0.0.1:8093/health"),
                   "sessions_endpoint": run("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8092/api/sessions")},
        "database": {"path": str(settings.DB_PATH),
                     "sha256": sha(settings.DB_PATH),
                     "counts": _counts(settings.DB_PATH)},
        "evidence_batches": evidence,
        "python": run(f"{ROOT}/.venv/bin/python -V"),
        "dependencies": run(f"{ROOT}/.venv/bin/pip list --format=freeze | grep -Ei '^(mcp|fastapi|httpx|uvicorn|pydantic|jsonschema)='"),
    }
    security.write_private(ROOT / "evidence" / "server-manifest.json",
                           json.dumps(payload, ensure_ascii=False, indent=1))
    print(json.dumps(payload["health"], ensure_ascii=False))


if __name__ == "__main__":
    main()
