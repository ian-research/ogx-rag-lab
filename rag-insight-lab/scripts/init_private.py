#!/usr/bin/env python3
"""Create the private admin credential for the observability lab (never overwritten)."""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

PRIVATE = Path(os.getenv("PRIVATE_DIR", "")).resolve()
if not PRIVATE or not str(PRIVATE).startswith("/"):
    print("PRIVATE_DIR must be an absolute path", file=sys.stderr)
    sys.exit(2)
PRIVATE.mkdir(parents=True, exist_ok=True)
os.chmod(PRIVATE, 0o700)

admins = PRIVATE / "admins.json"
creds = PRIVATE / "credentials-for-delivery.json"


def write_private(path: Path, payload: dict) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


if admins.exists():
    print("private/admins.json already exists; refusing to overwrite")
    sys.exit(1)

token = secrets.token_urlsafe(36)
digest = __import__("hashlib").sha256(token.encode()).hexdigest()
write_private(admins, {"ops-lab": {"sha256": digest, "role": "ops", "created": "2026-11-14"}})
write_private(creds, {"ops-lab": token})
print(f"admin credential written to {creds} (0600)")
