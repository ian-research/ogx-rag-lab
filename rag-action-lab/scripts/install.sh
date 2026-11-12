#!/usr/bin/env bash
# First installation of the article-5 services. Refuses to overwrite an existing setup.
set -euo pipefail
APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$APP_DIR"
STATE_DIR=${STATE_DIR:-/root/rag-lab/rag-action-state}
export STATE_DIR
source scripts/lib-units.sh

[[ $EUID -eq 0 ]] || die "install.sh must run as root"
[[ -x .venv/bin/python ]] || die ".venv is missing; run scripts/prepare.sh first"
[[ -s $STATE_DIR/private/sessions.json ]] || die "state is missing; run scripts/prepare.sh first"

for unit in "${UNITS[@]}"; do
  [[ -e $UNIT_DIR/$unit.service ]] && die "$unit.service already exists; use switch-services.sh --switch to upgrade"
done
[[ -e $CONFIG_PATH ]] && die "$CONFIG_PATH already exists; use switch-services.sh --switch to upgrade"

TMP=$(mktemp -d); CONFIG_TMP=$(mktemp)
CREATED=()
cleanup() {
  local rc=$?
  rm -rf "$TMP" "$CONFIG_TMP"
  if (( rc != 0 )); then
    echo "install failed; removing anything this run created" >&2
    for unit in "${CREATED[@]:-}"; do
      [[ -n $unit ]] || continue
      "$SYSTEMCTL" stop "$unit.service" >/dev/null 2>&1 || true
      "$SYSTEMCTL" disable "$unit.service" >/dev/null 2>&1 || true
      rm -f "$UNIT_DIR/$unit.service"
    done
    rm -f "$CONFIG_PATH"
    "$SYSTEMCTL" daemon-reload >/dev/null 2>&1 || true
  fi
  exit $rc
}
trap cleanup EXIT

# Render and verify before writing anything into the live locations.
for unit in "${UNITS[@]}"; do
  render_template "deploy/$unit.service.in" "$APP_DIR" "$TMP/$unit.service"
done
write_config "$APP_DIR" "$CONFIG_TMP"
install -d -m 755 "$UNIT_DIR"
install -d -m 755 "$(dirname "$CONFIG_PATH")" 2>/dev/null || true
install -m 640 -o root -g root "$CONFIG_TMP" "$CONFIG_PATH"
restorecon_config "$CONFIG_PATH"
# Verify from the rendered copies only after the config they reference exists; nothing is
# installed under $UNIT_DIR until every unit passes.
for unit in "${UNITS[@]}"; do
  systemd-analyze verify "$TMP/$unit.service" || die "systemd-analyze verify failed for $unit"
done
CREATED=("${UNITS[@]}")
for unit in "${UNITS[@]}"; do install -m 644 "$TMP/$unit.service" "$UNIT_DIR/$unit.service"; done
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now article05-action-mcp.service
"$SYSTEMCTL" enable --now article05-action-api.service

wait_healthy http://127.0.0.1:8093/health 45 || die "back office health check failed"
wait_healthy http://127.0.0.1:8092/api/health 45 || die "application health check failed"
echo "install: ok ($APP_DIR)"
