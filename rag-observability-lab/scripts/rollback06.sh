#!/usr/bin/env bash
# Stop and remove the article-6 units. State, configuration and the image stay.
# --purge also removes the configuration file (never the state directory).
set -euo pipefail
PURGE=${1:-}
CONFIG=${CONFIG_PATH:-/etc/rag-lab/article06-runtime.env}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
SYSTEMCTL=${SYSTEMCTL:-systemctl}

for unit in article06-api article06-mcp article06-jaeger; do
  ${SYSTEMCTL} disable --now "${unit}.service" 2>/dev/null || true
  rm -f "${UNIT_DIR}/${unit}.service"
done
${SYSTEMCTL} daemon-reload
if [[ ${PURGE} == "--purge" ]]; then
  rm -f "${CONFIG}"
  echo "[rollback06] units and configuration removed; state directory and image kept"
else
  echo "[rollback06] units removed; configuration and state kept (install06 adopts them)"
fi
