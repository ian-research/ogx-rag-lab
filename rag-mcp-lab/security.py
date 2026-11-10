"""A small lab credential boundary, not an enterprise login or document ACL."""
import hashlib
import json
import os
import secrets
from pathlib import Path

import settings


def private_text(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        raise RuntimeError("private configuration permissions must be 0600")
    return path.read_text(encoding="utf-8")


def session_identity(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    if not 32 <= len(token) <= 256:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    sessions = json.loads(private_text(settings.PRIVATE / "sessions.json"))
    for session in sessions:
        if secrets.compare_digest(session["token_sha256"], digest):
            return {k: session[k] for k in ("session_id", "label", "identity")}
    return None


def service_key():
    key = private_text(settings.PRIVATE / "mcp.key").strip()
    if len(key) < 32:
        raise RuntimeError("service key is not configured")
    return key


def service_authorized(value):
    return isinstance(value, str) and secrets.compare_digest(value, "Bearer " + service_key())


def write_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never silently overwrite an existing credential or an old evidence file.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(data)


def redact(value, credentials):
    """Exclude secrets even when a caller accidentally pastes one in a question."""
    if isinstance(value, str):
        for secret in credentials:
            if secret:
                value = value.replace(secret, "[凭据已隐藏]")
        return value
    if isinstance(value, list):
        return [redact(item, credentials) for item in value]
    if isinstance(value, dict):
        return {redact(str(k), credentials): redact(v, credentials) for k, v in value.items()}
    return value
