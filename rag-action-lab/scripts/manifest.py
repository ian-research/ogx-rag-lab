"""Write a deterministic file manifest for the package (no private data, no venv)."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {".venv", "__pycache__", "private", "data", "evidence", ".pytest_cache", ".git"}
EXCLUDE_NAMES = {"BUILD-MANIFEST.json"}


def files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(ROOT).parts)
        if parts & EXCLUDE_DIRS or path.name in EXCLUDE_NAMES:
            continue
        if path.name.startswith("private-"):
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
