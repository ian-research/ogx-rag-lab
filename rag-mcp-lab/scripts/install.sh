#!/usr/bin/env bash
# First-time install on a host with no article-04 units or config.
# Everything is rendered and verified before the first file is written; a failure
# before that point leaves nothing behind. A failure after the write phase stops and
# disables only the units this run installed, then removes the files it created.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run on the server as root' >&2; exit 1; }
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source scripts/lib-units.sh
APP_DIR=$PWD
[[ -x .venv/bin/python ]] || die 'Prepare the runtime first: bash scripts/prepare.sh'
[[ -f private/mcp.key ]] || die 'private/mcp.key missing; run scripts/prepare.sh'

for unit in "${UNITS[@]}"; do
  if [[ -e $UNIT_DIR/$unit.service ]]; then
    die "Existing unit $unit.service found. This is a first install entry point; use scripts/switch-services.sh --switch."
  fi
done
if [[ -e $CONFIG_PATH ]]; then die "$CONFIG_PATH already exists; refusing to overwrite a non-empty installation"; fi

# Health endpoints are overridable so the failure path can be exercised in a sandbox.
ORCH_HEALTH_URL=${ORCH_HEALTH_URL:-http://127.0.0.1:8091/api/health}
MCP_HEALTH_URL=${MCP_HEALTH_URL:-http://127.0.0.1:8090/health}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-60}

tmp=$(mktemp -d)
created_config=0
created_units=()
installed_names=()
started=0
cleanup_failed=0

cleanup() {
  set +e
  local rc=$1
  if (( rc != 0 )); then
    # Stop and disable only the units this run installed, and do it before deleting
    # the unit files so systemd still knows them. Never touch a pre-existing service.
    if (( started )); then
      local unit
      for unit in "${installed_names[@]}"; do
        "$SYSTEMCTL" stop "$unit" >/dev/null 2>&1
        "$SYSTEMCTL" disable "$unit" >/dev/null 2>&1
      done
      for unit in "${installed_names[@]}"; do
        if "$SYSTEMCTL" is-active --quiet "$unit" >/dev/null 2>&1; then cleanup_failed=1; fi
        if "$SYSTEMCTL" is-enabled --quiet "$unit" >/dev/null 2>&1; then cleanup_failed=1; fi
      done
    fi
    if (( ${#created_units[@]} > 0 )); then
      rm -f "${created_units[@]}" || cleanup_failed=1
      "$SYSTEMCTL" daemon-reload >/dev/null 2>&1 || cleanup_failed=1
    fi
    if (( created_config )); then rm -f "$CONFIG_PATH" || cleanup_failed=1; fi
    if (( cleanup_failed )); then
      echo "install failed (rc=$rc); cleanup was INCOMPLETE. Inspect and clean up manually:" >&2
      echo "  systemctl status ${installed_names[*]:-<none>}" >&2
      echo "  ls -l ${UNIT_DIR} ${CONFIG_PATH}" >&2
    else
      echo "install failed (rc=$rc); stopped and removed everything this run created. Re-run after fixing the cause." >&2
    fi
  fi
  rm -rf "$tmp"
  return 0
}
trap 'cleanup $?' EXIT

# 1. Render and verify everything first: no config is written before this succeeds.
write_config "$APP_DIR" "$tmp/article04-runtime.env"
rendered=()
for unit in "${UNITS[@]}"; do
  out="$tmp/$unit.service"
  render_template "deploy/$unit.service.in" "$APP_DIR" "$out"
  rendered+=("$out")
done
systemd-analyze verify "${rendered[@]}"

# 2. Write phase.
install -d -m 755 "$(dirname "$CONFIG_PATH")"
install -m 640 -o root -g root "$tmp/article04-runtime.env" "$CONFIG_PATH"
created_config=1
restorecon_config "$CONFIG_PATH"

install -d -m 755 "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  install -m 644 "$tmp/$unit.service" "$UNIT_DIR/$unit.service"
  created_units+=("$UNIT_DIR/$unit.service")
  installed_names+=("$unit")
done
"$SYSTEMCTL" daemon-reload
# From here on a failure may leave running processes, so cleanup has to stop them.
started=1
"$SYSTEMCTL" enable --now "${UNITS[@]}"
wait_healthy "$ORCH_HEALTH_URL" "$HEALTH_TIMEOUT" || die 'Orchestrator did not become healthy'
wait_healthy "$MCP_HEALTH_URL" "$HEALTH_TIMEOUT" || die 'MCP service did not become healthy'

echo "First install complete. Config: $CONFIG_PATH"
echo 'Run scripts/verify.py --phase baseline --as-of <date> before claiming business behaviour.'
