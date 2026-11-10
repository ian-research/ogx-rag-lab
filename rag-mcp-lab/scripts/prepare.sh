#!/usr/bin/env bash
# Server only. Prepare a separate runtime; do not stop services or alter vLLM.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PYTHON_BIN=${PYTHON_BIN:-python3.12}
"$PYTHON_BIN" -c 'import sys; assert (3,12) <= sys.version_info[:2] < (3,14), "Use Python 3.12 or 3.13"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip check
if [[ ! -d private ]]; then
  .venv/bin/python init_private.py
fi
.venv/bin/python -c 'from security import service_key, session_identity; service_key(); import mcp_business_server, orchestrator; print("Runtime imports and private service key checked")'
echo 'Prepared only. Read DEPLOYMENT.txt before switching systemd units.'
