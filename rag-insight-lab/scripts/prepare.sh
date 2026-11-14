#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR=${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
STATE_DIR=${STATE_DIR:-/root/rag-lab/article07-state}
PY=${PYTHON_BIN:-python3.12}
cd "$APP_DIR"
"$PY" -c 'import sys; assert (3,12) <= sys.version_info[:2] < (3,14), sys.version' || { echo "need python 3.12/3.13"; exit 2; }
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet --require-hashes -r requirements.lock
./.venv/bin/python -m pip check
install -d -m 700 "$STATE_DIR" "$STATE_DIR/private" "$STATE_DIR/data" "$STATE_DIR/evidence" "$STATE_DIR/runs"
if [ ! -f "$STATE_DIR/private/admins.json" ]; then
  PRIVATE_DIR="$STATE_DIR/private" ./.venv/bin/python scripts/init_private.py
else
  echo "admin credential already present (not overwritten)"
fi
./.venv/bin/python - <<'PY'
import importlib
for m in ("fastapi", "uvicorn", "httpx", "pydantic", "mcp"):
    mod = importlib.import_module(m)
    print(m, getattr(mod, "__version__", "?"))
PY
