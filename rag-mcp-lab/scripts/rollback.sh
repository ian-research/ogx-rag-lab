#!/usr/bin/env bash
# Restore an explicit backup (units, config, unit states). Does not touch vLLM, data or evidence.
# v3 backups record config_action (restored / left_in_place); v1/v2 are inferred.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run on the server as root' >&2; exit 1; }
[[ $# -eq 1 && -d $1 ]] || { echo 'Usage: bash scripts/rollback.sh /absolute/path/to/unit-backup' >&2; exit 1; }
source "$(dirname -- "${BASH_SOURCE[0]}")/lib-units.sh"
backup=$1
ready=$(cat "$backup/READY" 2>/dev/null || true)
[[ $ready == article04-unit-backup-v1 || $ready == article04-unit-backup-v2 || $ready == article04-unit-backup-v3 ]] \
  || { echo 'Incomplete or unknown backup' >&2; exit 1; }

for unit in "${UNITS[@]}"; do
  [[ -s $backup/$unit.service && -f $backup/$unit.active && -f $backup/$unit.enabled ]] || die "Backup incomplete for $unit"
  enabled=$(cat "$backup/$unit.enabled")
  [[ $enabled == enabled || $enabled == disabled ]] || die "Unsupported original enable state: $enabled"
  active=$(cat "$backup/$unit.active")
  [[ $active == active || $active == inactive || $active == failed ]] || die "Unsupported original runtime state: $active"
done

config_action=left_in_place
if [[ -f $backup/config_action ]]; then
  config_action=$(cat "$backup/config_action")
elif [[ -f $backup/config.present ]]; then
  case $(cat "$backup/config.present") in
    present) config_action=restored ;;
    absent) config_action=left_in_place ;;
    *) die 'Unknown config state in backup' ;;
  esac
fi
[[ $config_action == restored || $config_action == left_in_place ]] || die "Unsupported config action: $config_action"
if [[ $config_action == restored ]]; then
  [[ -s $backup/article04-runtime.env ]] || die 'Backup claims a config but the copy is missing'
fi

systemd-analyze verify "$backup/article04-mcp.service" "$backup/article04-orchestrator.service"
"$SYSTEMCTL" stop article04-orchestrator article04-mcp
install -m 644 "$backup/article04-mcp.service" "$backup/article04-orchestrator.service" $UNIT_DIR/
if [[ $config_action == restored ]]; then
  install -d -m 755 "$(dirname "$CONFIG_PATH")"
  install -m 640 -o root -g root "$backup/article04-runtime.env" "$CONFIG_PATH"
  restorecon_config "$CONFIG_PATH"
fi
"$SYSTEMCTL" daemon-reload
for unit in "${UNITS[@]}"; do
  if [[ $(cat "$backup/$unit.enabled") == enabled ]]; then "$SYSTEMCTL" enable "$unit" >/dev/null; else "$SYSTEMCTL" disable "$unit" >/dev/null 2>&1 || true; fi
  if [[ $(cat "$backup/$unit.active") == active ]]; then
    "$SYSTEMCTL" start "$unit"
    "$SYSTEMCTL" is-active --quiet "$unit" || die "$unit did not return to active"
  fi
done
if [[ $config_action == restored ]]; then
  echo 'Units and config restored from the backup.'
else
  echo 'Units restored; this backup has no config copy, so the current config was left in place.'
  echo "Current config: $CONFIG_PATH (not reverted; check PRIVATE_DIR/RUN_ROOT before trusting the rolled-back units)."
fi
echo 'Verify the original API and one real query before declaring recovery.'
