"""Capture code/dependency versions without credentials or host addresses."""
import hashlib
import importlib.metadata
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {'.venv', '.venv-test', 'private', 'evidence', 'logs', '__pycache__', '.pytest_cache', '.git'}
EXCLUDE_PREFIX = ('private-',)


def manifest():
    hashes = {}
    for path in sorted(ROOT.rglob('*')):
        rel = path.relative_to(ROOT)
        if not path.is_file() or any(p in EXCLUDE for p in rel.parts) or path.name == 'BUILD-MANIFEST.json':
            continue
        if any(part.startswith(EXCLUDE_PREFIX) for part in rel.parts):
            continue
        hashes[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'captured_at': datetime.now(timezone.utc).isoformat(),
            'python': sys.version, 'platform': platform.system(), 'architecture': platform.machine(),
            'dependencies': {p: importlib.metadata.version(p) for p in ('mcp', 'fastapi', 'httpx', 'uvicorn', 'pydantic', 'jsonschema')},
            'upstream_commit': '7fc221dc7991a3d51432af4202fff6dae94b8d93', 'files_sha256': hashes,
            'model_revision': None, 'server_validation': 'not_recorded_by_this_manifest'}


if __name__ == '__main__':
    print(json.dumps(manifest(), ensure_ascii=False, indent=2))
