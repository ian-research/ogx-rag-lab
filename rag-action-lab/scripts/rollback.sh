#!/usr/bin/env bash
# Restore units and configuration from a backup produced by switch-services.sh.
set -euo pipefail
APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$APP_DIR"
STATE_DIR=${STATE_DIR:-/root/rag-lab/rag-action-state}
export STATE_DIR
source scripts/lib-units.sh

BACKUP=${1:?usage: rollback.sh <absolute backup directory>}
[[ $EUID -eq 0 ]] || die "rollback.sh must run as root"
[[ -r $BACKUP/READY ]] || die "not a backup directory: $BACKUP"
grep -qx 'READY=article05-unit-backup-v1' "$BACKUP/READY" || die "unknown backup marker"
ACTION=$(sed -n 's/^config_action=//p' "$BACKUP/READY")
[[ $ACTION == restored || $ACTION == absent ]] || die "invalid config_action: $ACTION"

for unit in "${UNITS[@]}"; do
  [[ -r $BACKUP/$unit.service ]] || die "backup is missing $unit.service"
  install -m 644 "$BACKUP/$unit.service" "$UNIT_DIR/$unit.service"
done
if [[ $ACTION == restored ]]; then
  install -m 640 -o root -g root "$BACKUP/runtime.env" "$CONFIG_PATH"
  restorecon_config "$CONFIG_PATH"
else
  echo "backup recorded no configuration; leaving $CONFIG_PATH untouched"
fi
"$SYSTEMCTL" daemon-reload
for unit in "${UNITS[@]}"; do
  state=$(cat "$BACKUP/$unit.enabled" 2>/dev/null || echo "")
  case "$state" in enabled*) "$SYSTEMCTL" enable "$unit.service" >/dev/null 2>&1 || true;; esac
  active=$(cat "$BACKUP/$unit.active" 2>/dev/null || echo "")
  if [[ $active == active ]]; then "$SYSTEMCTL" restart "$unit.service"; fi
done
wait_healthy http://127.0.0.1:8092/api/health 30 || die "application is not healthy after rollback"
echo "rollback: ok ($BACKUP, config_action=$ACTION)"
