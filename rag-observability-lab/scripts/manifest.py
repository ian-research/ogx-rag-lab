"""Write a deterministic file manifest for the package (no private data, no venv)."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {"__pycache__", "private", "data", "evidence", ".pytest_cache", ".git",
                "logs", "export", "runs"}
EXCLUDE_NAMES = {"BUILD-MANIFEST.json", "server-manifest.json"}
ROOT_FILES = {
    "action_api.py", "mcp_action_server.py", "model_loop.py", "docsearch.py",
    "draft.py", "store.py", "security.py", "settings.py", "telemetry.py",
    "metrics.py", "devices.py", "upstream_devices.json", "init_private.py",
    "requirements.lock", "requirements-otel.in", "requirements-otel.lock",
    "requirements-test.in", "requirements-test.lock", "README.txt", "DEPLOYMENT.txt",
    "README.md", "DEPLOYMENT.md", "TESTING.md", "PROVENANCE.json", ".gitignore",
    "LICENSE", "LICENSE-UPSTREAM",
}
SOURCE_DIRS = {"deploy", "docs", "web", "tests", "scripts"}


def files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if len(relative.parts) == 1 and path.name not in ROOT_FILES:
            continue
        if len(relative.parts) > 1 and relative.parts[0] not in SOURCE_DIRS:
            continue
        parts = set(relative.parts)
        if parts & EXCLUDE_DIRS or path.name in EXCLUDE_NAMES:
            continue
        if any(part.startswith((".venv", "._", "install-backup-")) for part in relative.parts):
            continue
        if path.name.startswith("private-") or path.suffix in {".pyc", ".pyo", ".key", ".pem"}:
            continue
        yield path


def main():
    entries = {}
    for path in files():
        entries[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"generated_by": "scripts/manifest.py", "file_count": len(entries),
                "files_sha256": entries}
    (ROOT / "BUILD-MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"manifest: {len(entries)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
