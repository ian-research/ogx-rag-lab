#!/usr/bin/env bash
# Upgrade path: switch the two article-04 units to this directory.
# Renders and validates before stopping anything; never writes a blank config.
#   default            migrate the existing config: update PRIVATE_DIR/RUN_ROOT, keep custom keys
#   --keep-config      keep the existing config untouched; its paths must already point here
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run on the server as root' >&2; exit 1; }
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source scripts/lib-units.sh
APP_DIR=$PWD

keep_config=0
args=("$@")
[[ ${#args[@]} -ge 1 && ${args[0]} == --switch ]] || { echo 'Usage: bash scripts/switch-services.sh --switch [--keep-config]' >&2; exit 1; }
for extra in "${args[@]:1}"; do
  case $extra in
    --keep-config) keep_config=1 ;;
    *) die "Unknown option: $extra" ;;
  esac
done

[[ -x .venv/bin/python ]] || die 'Prepare the runtime first: bash scripts/prepare.sh'
[[ -f private/mcp.key ]] || die 'private/mcp.key missing; run scripts/prepare.sh'

for unit in "${UNITS[@]}"; do
  original="$UNIT_DIR/$unit.service"
  [[ -f $original && ! -L $original ]] || die "Expected existing regular unit: $original (first install: use scripts/install.sh)"
done

# Render and validate BEFORE touching the running services.
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
rendered=()
for unit in "${UNITS[@]}"; do
  out="$tmp/$unit.service"
  render_template "deploy/$unit.service.in" "$APP_DIR" "$out"
  rendered+=("$out")
done
systemd-analyze verify "${rendered[@]}"

new_config=0
if (( keep_config )); then
  [[ -s $CONFIG_PATH ]] || die "--keep-config requested but $CONFIG_PATH is missing or empty"
  config_paths_ok "$CONFIG_PATH" "$APP_DIR" || die "--keep-config but $CONFIG_PATH does not point PRIVATE_DIR/RUN_ROOT at $APP_DIR; drop --keep-config to migrate, or fix the config first"
else
  if [[ -e $CONFIG_PATH ]]; then
    migrate_config "$CONFIG_PATH" "$APP_DIR" "$tmp/article04-runtime.env"
    echo "Config migrated from $CONFIG_PATH (custom keys kept, PRIVATE_DIR/RUN_ROOT updated)."
  else
    write_config "$APP_DIR" "$tmp/article04-runtime.env"
  fi
  new_config=1
fi

backup="private/units-before-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$backup"
for unit in "${UNITS[@]}"; do
  cp -p "$UNIT_DIR/$unit.service" "$backup/$unit.service"
  "$SYSTEMCTL" is-active "$unit" > "$backup/$unit.active" || true
  "$SYSTEMCTL" is-enabled "$unit" > "$backup/$unit.enabled" || true
done
if [[ -f $CONFIG_PATH ]]; then
  install -m 600 "$CONFIG_PATH" "$backup/article04-runtime.env"
  printf 'present\n' > "$backup/config.present"
  printf 'restored\n' > "$backup/config_action"
else
  printf 'absent\n' > "$backup/config.present"
  printf 'left_in_place\n' > "$backup/config_action"
fi
printf '%s\n' 'article04-unit-backup-v3' > "$backup/READY"

before_active_mcp=$("$SYSTEMCTL" is-active article04-mcp || true)
"$SYSTEMCTL" stop article04-orchestrator article04-mcp
install -m 644 "${rendered[@]}" $UNIT_DIR/
if (( new_config )); then
  install -d -m 755 "$(dirname "$CONFIG_PATH")"
  install -m 640 -o root -g root "$tmp/article04-runtime.env" "$CONFIG_PATH"
  restorecon_config "$CONFIG_PATH"
fi
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now "${UNITS[@]}"
wait_healthy http://127.0.0.1:8091/api/health 60 || die 'Orchestrator did not become healthy after switch'
wait_healthy http://127.0.0.1:8090/health 60 || die 'MCP service did not become healthy after switch'
printf 'Units switched to %s. Backup: %s (previous mcp state: %s, config: %s)\n' \
  "$APP_DIR" "$backup" "$before_active_mcp" "$( [[ $new_config == 1 ]] && echo written || echo kept )"
echo 'Run scripts/verify.py; an active unit alone is not a successful business test.'
