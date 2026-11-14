#!/usr/bin/env bash
# Remove this article's two units. --purge also removes config; state is kept.
set -euo pipefail

UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
STATE_DIR="${STATE_DIR:-/root/rag-lab/article07-state}"
CONFIG_PATH="${CONFIG_PATH:-/etc/rag-lab/article07-runtime.env}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

log() { printf '[rollback07] %s\n' "$*"; }

for unit in article07-api.service article07-prometheus.service; do
  systemctl disable --now "$unit" 2>/dev/null || true
  rm -f "$UNIT_DIR/$unit"
done
systemctl daemon-reload 2>/dev/null || true

if [ "$PURGE" = 1 ]; then
  rm -f "$CONFIG_PATH"
  log "removed config: $CONFIG_PATH"
else
  log "kept config: $CONFIG_PATH"
fi
log "kept state: $STATE_DIR (evidence, raw returns, runs)"
