#!/usr/bin/env bash
# Upgrade an existing installation: backup, render, verify, stop, install, start, check.
set -euo pipefail
APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$APP_DIR"
STATE_DIR=${STATE_DIR:-/root/rag-lab/rag-action-state}
export STATE_DIR
source scripts/lib-units.sh

[[ ${1:-} == --switch ]] || die "usage: switch-services.sh --switch [--keep-config]"
KEEP_CONFIG=0
[[ ${2:-} == --keep-config || ${1:-} == --keep-config ]] && KEEP_CONFIG=1
[[ $EUID -eq 0 ]] || die "switch-services.sh must run as root"
[[ -x .venv/bin/python ]] || die ".venv is missing"
for unit in "${UNITS[@]}"; do
  [[ -e $UNIT_DIR/$unit.service ]] || die "$unit.service is not installed; use install.sh for the first install"
done
if (( KEEP_CONFIG )); then
  config_paths_ok "$CONFIG_PATH" "$APP_DIR" || die "existing config does not belong to $APP_DIR; refusing before stopping services"
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=$APP_DIR/private/units-before-$STAMP
install -d -m 700 "$BACKUP"
for unit in "${UNITS[@]}"; do
  cp "$UNIT_DIR/$unit.service" "$BACKUP/$unit.service"
  "$SYSTEMCTL" is-active "$unit.service" > "$BACKUP/$unit.active" 2>/dev/null || true
  "$SYSTEMCTL" is-enabled "$unit.service" > "$BACKUP/$unit.enabled" 2>/dev/null || true
done
CFG_ACTION=absent
if [[ -e $CONFIG_PATH ]]; then cp "$CONFIG_PATH" "$BACKUP/runtime.env"; CFG_ACTION=restored; fi
echo "READY=article05-unit-backup-v1" > "$BACKUP/READY"
echo "config_action=$CFG_ACTION" >> "$BACKUP/READY"

TMP=$(mktemp -d); CONFIG_TMP=$(mktemp)
trap 'rm -rf "$TMP" "$CONFIG_TMP"' EXIT
for unit in "${UNITS[@]}"; do
  render_template "deploy/$unit.service.in" "$APP_DIR" "$TMP/$unit.service"
  systemd-analyze verify "$TMP/$unit.service" || die "verify failed for $unit"
done
if (( KEEP_CONFIG )); then
  cp "$CONFIG_PATH" "$CONFIG_TMP"
else
  if [[ -e $CONFIG_PATH ]]; then migrate_config "$CONFIG_PATH" "$APP_DIR" "$CONFIG_TMP"
  else write_config "$APP_DIR" "$CONFIG_TMP"; fi
fi

for unit in "${UNITS[@]}"; do "$SYSTEMCTL" stop "$unit.service" || true; done
for unit in "${UNITS[@]}"; do install -m 644 "$TMP/$unit.service" "$UNIT_DIR/$unit.service"; done
install -m 640 -o root -g root "$CONFIG_TMP" "$CONFIG_PATH"
restorecon_config "$CONFIG_PATH"
"$SYSTEMCTL" daemon-reload
for unit in "${UNITS[@]}"; do "$SYSTEMCTL" enable "$unit.service" >/dev/null 2>&1 || true; done
"$SYSTEMCTL" start article05-action-mcp.service
"$SYSTEMCTL" start article05-action-api.service
if ! wait_healthy http://127.0.0.1:8093/health 45 || ! wait_healthy http://127.0.0.1:8092/api/health 45; then
  echo "health check failed; restoring $BACKUP" >&2
  scripts/rollback.sh "$BACKUP" || true
  die "switch failed"
fi
echo "switch: ok (backup $BACKUP, config_action=$CFG_ACTION)"
