#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec "${PYTHON_BIN:-python3}" "$APP_DIR/scripts/package07.py" "$@"
