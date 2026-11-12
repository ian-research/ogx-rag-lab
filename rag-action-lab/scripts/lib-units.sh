#!/usr/bin/env bash
# Shared helpers for rendering units/config. Sourced, never executed directly.
CONFIG_PATH=${CONFIG_PATH:-/etc/rag-lab/article05-runtime.env}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
UNITS=(article05-action-api article05-action-mcp)
STATE_DIR=${STATE_DIR:-/root/rag-lab/rag-action-state}
REQUIRED_KEYS=(OGX_BASE OGX_MODEL DOC_SERVICE ACTION_MCP_URL PUBLIC_BASE STATE_DIR PRIVATE_DIR DATA_DIR DB_PATH
               RUN_ROOT DEFAULT_POLICY DRAFT_TTL_SECONDS CONFIRM_TTL_SECONDS SUBMIT_TIMEOUT)

die() { echo "$*" >&2; exit 1; }

render_template() {  # $1 template, $2 app dir, $3 output
  sed -e "s|@APP_DIR@|$2|g" -e "s|@STATE_DIR@|$STATE_DIR|g" -e "s|@CONFIG_PATH@|$CONFIG_PATH|g" "$1" > "$3"
  if grep -q '@APP_DIR@\|@STATE_DIR@\|@CONFIG_PATH@' "$3"; then die "Unrendered placeholder in $3"; fi
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
  [[ -s $2 ]] || die 'Rendered config is empty'
  grep -qxF "PRIVATE_DIR=$STATE_DIR/private" "$2" || die 'Rendered config does not point PRIVATE_DIR at the state dir'
  grep -qxF "DB_PATH=$STATE_DIR/data/action.db" "$2" || die 'Rendered config does not point DB_PATH at the state dir'
  require_keys "$2"
}

migrate_config() {  # $1 existing config, $2 app dir, $3 output
  [[ -r $1 ]] || die "Existing config is not readable: $1"
  # Persistent state is shared between revisions: an upgrade must never repoint
  # the database or the signing key at the new code directory. A config written
  # before STATE_DIR existed is adopted into the state directory in use now.
  local state
  state=$(sed -n 's/^STATE_DIR=//p' "$1")
  if [[ -z $state ]]; then state=$STATE_DIR; fi
  if [[ ! -d $state ]]; then die "State directory does not exist: $state"; fi
  STATE_DIR=$state
  sed -e "s|^STATE_DIR=.*|STATE_DIR=$state|" \
      -e "s|^PRIVATE_DIR=.*|PRIVATE_DIR=$state/private|" \
      -e "s|^DATA_DIR=.*|DATA_DIR=$state/data|" \
      -e "s|^DB_PATH=.*|DB_PATH=$state/data/action.db|" \
      -e "s|^RUN_ROOT=.*|RUN_ROOT=$state/evidence/runs|" "$1" > "$3"
  if ! grep -q '^STATE_DIR=' "$3"; then sed -i "1i STATE_DIR=$state" "$3"; fi
  grep -qxF "PRIVATE_DIR=$state/private" "$3" || die 'Config migration did not keep PRIVATE_DIR in the state dir'
  grep -qxF "DB_PATH=$state/data/action.db" "$3" || die 'Config migration did not keep DB_PATH in the state dir'
  require_keys "$3"
}

config_paths_ok() {  # $1 config, $2 app dir (kept for callers; state is authoritative)
  [[ -r $1 ]] || return 1
  grep -qxF "PRIVATE_DIR=$STATE_DIR/private" "$1" && grep -qxF "DB_PATH=$STATE_DIR/data/action.db" "$1"
}

restorecon_config() {  # $1 config path
  if command -v restorecon >/dev/null 2>&1; then
    restorecon -v "$1" | sed 's/^/restorecon: /'
  else
    echo "restorecon: not available, skipped"
  fi
}

wait_healthy() {  # $1 url, $2 seconds
  local url=$1 deadline=$((SECONDS + $2))
  while ((SECONDS < deadline)); do
    curl -fsS --max-time 3 "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
