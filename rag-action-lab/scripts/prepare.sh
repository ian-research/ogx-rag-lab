#!/usr/bin/env bash
# Prepare a revision: virtualenv + locked dependencies + private state.
# Persistent state (SQLite, keys, run records) lives outside the code directory and
# is reused across revisions; this script never regenerates existing keys.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN=${PYTHON_BIN:-python3.12}
STATE_DIR=${STATE_DIR:-/root/rag-lab/rag-action-state}
"$PYTHON_BIN" -c 'import sys; assert (3,12) <= sys.version_info[:2] < (3,14), sys.version' \
  || { echo "PYTHON_BIN must be 3.12 or 3.13" >&2; exit 1; }
[[ -d .venv ]] || "$PYTHON_BIN" -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q --require-hashes -r requirements.lock
./.venv/bin/pip check

install -d -m 700 "$STATE_DIR"
install -d -m 700 "$STATE_DIR/private" "$STATE_DIR/data"
install -d -m 700 "$STATE_DIR/evidence" "$STATE_DIR/evidence/runs"
if [[ -s $STATE_DIR/private/sessions.json ]]; then
  echo "prepare: reusing existing private state in $STATE_DIR/private (keys unchanged)"
else
  PRIVATE_DIR="$STATE_DIR/private" ./.venv/bin/python init_private.py
fi
if [[ -s $STATE_DIR/data/action.db ]]; then
  ./.venv/bin/python scripts/check_state.py "$STATE_DIR" || { echo "state check failed" >&2; exit 1; }
fi
PRIVATE_DIR="$STATE_DIR/private" DATA_DIR="$STATE_DIR/data" \
  DB_PATH="$STATE_DIR/data/action.db" RUN_ROOT="$STATE_DIR/evidence/runs" \
  ./.venv/bin/python -c 'import action_api, mcp_action_server, draft, store, security; print("imports ok")'
echo "prepare: ok ($STATE_DIR)"
