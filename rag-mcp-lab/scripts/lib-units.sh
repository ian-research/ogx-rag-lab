#!/usr/bin/env bash
# Shared helpers for rendering units/config. Sourced, never executed directly.
CONFIG_PATH=${CONFIG_PATH:-/etc/rag-lab/article04-runtime.env}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
UNITS=(article04-mcp article04-orchestrator)
REQUIRED_KEYS=(OGX_BASE OGX_MODEL MCP_URL DOC_SERVICE PRIVATE_DIR RUN_ROOT MAX_STEPS MAX_TOOL_CALLS
               REQUEST_TIMEOUT MODEL_TIMEOUT TOOL_TIMEOUT MAX_TOOL_BYTES MAX_TOTAL_TOOL_BYTES)

die() { echo "$*" >&2; exit 1; }

render_template() {  # $1 template, $2 app dir, $3 output
  sed -e "s|@APP_DIR@|$2|g" -e "s|@CONFIG_PATH@|$CONFIG_PATH|g" "$1" > "$3"
  if grep -q '@APP_DIR@\|@CONFIG_PATH@' "$3"; then die "Unrendered placeholder in $3"; fi
}

require_keys() {  # $1 config file
  local missing=() key
  for key in "${REQUIRED_KEYS[@]}"; do
    grep -q "^$key=" "$1" || missing+=("$key")
  done
  (( ${#missing[@]} == 0 )) || die "Config is missing keys: ${missing[*]}"
}

write_config() {  # $1 app dir, $2 output
  render_template deploy/runtime.env.in "$1" "$2"
  if [[ ! -s $2 ]]; then die 'Rendered config is empty'; fi
  if ! grep -qxF "PRIVATE_DIR=$1/private" "$2"; then
    die 'Rendered config does not point PRIVATE_DIR at the new app dir'
  fi
  require_keys "$2"
}

migrate_config() {  # $1 existing config, $2 app dir, $3 output
  [[ -r $1 ]] || die "Existing config is not readable: $1"
  require_keys "$1"
  sed -e "s|^PRIVATE_DIR=.*|PRIVATE_DIR=$2/private|" -e "s|^RUN_ROOT=.*|RUN_ROOT=$2/evidence/runs|" "$1" > "$3"
  if ! grep -qxF "PRIVATE_DIR=$2/private" "$3" || ! grep -qxF "RUN_ROOT=$2/evidence/runs" "$3"; then
    die 'Config migration did not update PRIVATE_DIR/RUN_ROOT'
  fi
  require_keys "$3"
}

config_paths_ok() {  # $1 config, $2 app dir
  [[ -r $1 ]] || return 1
  grep -qxF "PRIVATE_DIR=$2/private" "$1" && grep -qxF "RUN_ROOT=$2/evidence/runs" "$1"
}

restorecon_config() {  # $1 config path; keeps the label that systemd can read under Enforcing
  if command -v restorecon >/dev/null 2>&1; then
    restorecon -v "$1" | sed 's/^/restorecon: /'
  else
    echo "restorecon: not available, skipped"
  fi
}

wait_healthy() {  # $1 url, $2 seconds
  local url=$1 deadline=$((SECONDS + $2))
  while ((SECONDS < deadline )); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}
