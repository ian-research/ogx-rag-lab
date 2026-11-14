"""Read-only metadata for the article-6 instances.

Collects units, ports, process locations, configuration metadata and source
hashes. It never opens the private directory, session files or the database,
so the result can be handed over without redaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = pathlib.Path(os.getenv("CONFIG_PATH", "/etc/rag-lab/article06-runtime.env"))
UNIT_DIR = pathlib.Path(os.getenv("UNIT_DIR", "/etc/systemd/system"))
OUT = pathlib.Path(os.getenv("MANIFEST_OUT", str(ROOT / "evidence" / "server-manifest.json")))

UNITS = ("article06-api.service", "article06-mcp.service", "article06-jaeger.service")
PORTS = (8094, 8095, 16686, 4317, 4318)
SOURCE_SUFFIXES = {".py", ".sh", ".html", ".txt", ".in", ".lock", ".md", ".json"}
SKIP_DIRS = {".venv", ".venv-test", "private", "data", "evidence", "__pycache__", ".git", ".pytest_cache"}


def run(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15, check=False).stdout.strip()
    except Exception as exc:  # noqa: BLE001 - metadata only
        return f"<error: {type(exc).__name__}>"


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        # macOS metadata siblings (._name) appear when a tree is copied from a Mac.
        if path.name.startswith("._") or any(part.startswith("._") for part in relative.parts):
            continue
        if path.suffix not in SOURCE_SUFFIXES:
            continue
        result[str(path.relative_to(ROOT))] = sha256(path)
    return result


def config_info() -> dict:
    if not CONFIG_PATH.exists():
        return {"present": False}
    stat = CONFIG_PATH.stat()
    mode = oct(stat.st_mode & 0o777)
    context = run("stat", "-c", "%C", str(CONFIG_PATH))
    text = CONFIG_PATH.read_text(errors="ignore")
    keys = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0])
    secret_like = [key for key in keys if any(word in key.upper() for word in ("SECRET", "TOKEN", "PASSWORD", "KEY"))]
    return {"present": True, "mode": mode, "selinux": context, "keys": sorted(keys), "secret_like_keys": secret_like}


def units_info() -> dict:
    result = {}
    for unit in UNITS:
        path = UNIT_DIR / unit
        result[unit] = {
            "present": path.exists(),
            "sha256": sha256(path) if path.exists() else None,
            "enabled": run("systemctl", "is-enabled", unit) or None,
            "active": run("systemctl", "is-active", unit) or None,
            "main_pid": run("systemctl", "show", "-p", "MainPID", "--value", unit) or None,
            "exec_start": run("systemctl", "show", "-p", "ExecStart", "--value", unit) or None,
        }
        pid = result[unit]["main_pid"]
        if pid and pid.isdigit() and int(pid) > 0:
            result[unit]["cwd"] = run("readlink", f"/proc/{pid}/cwd")
            result[unit]["exe"] = run("readlink", f"/proc/{pid}/exe")
    return result


def ports_info() -> dict:
    text = run("ss", "-ltnH")
    result = {}
    for port in PORTS:
        hits = [line.split() for line in text.splitlines() if f":{port}" in line]
        result[str(port)] = [
            {"local": row[3], "listen": True} for row in hits if len(row) >= 4
        ]
    return result


def packages() -> dict[str, str]:
    return {dist.metadata["Name"]: dist.version for dist in metadata.distributions() if dist.metadata["Name"]}


def main() -> int:
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(),
        "kernel": platform.release(),
        "python": sys.version.split()[0],
        "app_dir": str(ROOT),
        "config": config_info(),
        "units": units_info(),
        "ports": ports_info(),
        "source": source_hashes(),
        "packages": packages(),
        "notes": [
            "Read-only: does not open private/, data/ or the database.",
            "Source hashes cover application, scripts, deploy templates and lock files.",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    print(json.dumps({"out": str(OUT), "files": len(manifest["source"]),
                      "units": {k: v["active"] for k, v in manifest["units"].items()}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
