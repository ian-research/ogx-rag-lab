import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import settings
from security import write_private


@pytest.fixture
def private_config(tmp_path, monkeypatch):
    private = tmp_path / "private"
    tokens = {"a": "local-test-a-" + "A" * 36, "b": "local-test-b-" + "B" * 36,
              "empty": "local-test-empty-" + "E" * 36}
    identities = {"a": "alice.johnson@company.com", "b": "john.doe@company.com", "empty": "empty@example.invalid"}
    sessions = [{"session_id": name, "label": name, "identity": identities[name],
                 "token_sha256": hashlib.sha256(token.encode()).hexdigest()} for name, token in tokens.items()]
    write_private(private / "sessions.json", json.dumps(sessions))
    write_private(private / "mcp.key", "local-service-key-" + "K" * 36)
    monkeypatch.setattr(settings, "PRIVATE", private)
    monkeypatch.setattr(settings, "RUN_ROOT", tmp_path / "runs")
    return tokens
