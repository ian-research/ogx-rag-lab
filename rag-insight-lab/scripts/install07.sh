#!/usr/bin/env bash
# Install the article-07 observability lab. Conservative: refuses to overwrite,
# cleans up only what it created, never touches the other article services.
set -Eeuo pipefail

APP_DIR=${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
STATE_DIR=${STATE_DIR:-/root/rag-lab/article07-state}
CONFIG_PATH=${CONFIG_PATH:-/etc/rag-lab/article07-runtime.env}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
API_PORT=${API_PORT:-8096}
PROM_PORT=${PROM_PORT:-9090}
METRICS_PORT=${METRICS_PORT:-8094}
PROM_IMAGE=${PROM_IMAGE:-sha256:a3bc50fcb50fb1409287c32773d8fe799f6cad5511c4ba3e2e851784e7a05026}
JAEGER_MCP_URL=${JAEGER_MCP_URL:-http://127.0.0.1:16686/api/ai/mcp/}
JAEGER_QUERY_URL=${JAEGER_QUERY_URL:-http://127.0.0.1:16686}
PROM_URL=${PROM_URL:-http://127.0.0.1:9090}
OGX_BASE=${OGX_BASE:-http://127.0.0.1:8321}
OGX_MODEL=${OGX_MODEL:-local-vllm/rag-llm}
ALLOW_NONROOT=${ALLOW_NONROOT:-0}
SKIP_PREFLIGHT=${SKIP_PREFLIGHT:-0}
REPLACE=${ARTICLE07_REPLACE:-0}

API_UNIT="$UNIT_DIR/article07-api.service"
PROM_UNIT="$UNIT_DIR/article07-prometheus.service"
CREATED_CONFIG=0
CREATED_PROM_CFG=0
CREATED_API_UNIT=0
CREATED_PROM_UNIT=0
STARTED_API=0
STARTED_PROM=0
BACKUP_DIR=""
TMP=""

log() { printf '[install07] %s\n' "$*"; }
fail() { printf '[install07] ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
  local rc=$1
  [ -n "$TMP" ] && [ -d "$TMP" ] && rm -rf "$TMP"
  if [ "$rc" -ne 0 ]; then
    log "installation failed (rc=$rc); removing resources created by this run"
    # Only stop what this run actually started.
    if [ "$STARTED_API" = 1 ]; then systemctl stop article07-api.service 2>/dev/null || true; fi
    if [ "$STARTED_PROM" = 1 ]; then systemctl stop article07-prometheus.service 2>/dev/null || true; fi
    [ "$CREATED_API_UNIT" = 1 ] && rm -f "$API_UNIT"
    [ "$CREATED_PROM_UNIT" = 1 ] && rm -f "$PROM_UNIT"
    [ "$CREATED_CONFIG" = 1 ] && rm -f "$CONFIG_PATH"
    [ "$CREATED_PROM_CFG" = 1 ] && rm -f "$STATE_DIR/prometheus-config/prometheus.yml"
    systemctl daemon-reload 2>/dev/null || true
    if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
      log "restoring previous units from $BACKUP_DIR"
      cp -f "$BACKUP_DIR"/article07-*.service "$UNIT_DIR"/ 2>/dev/null || true
      if [ -f "$BACKUP_DIR/prometheus.yml" ]; then
        install -d -m 755 "$STATE_DIR/prometheus-config"
        cp -f "$BACKUP_DIR/prometheus.yml" "$STATE_DIR/prometheus-config/prometheus.yml"
      fi
      systemctl daemon-reload 2>/dev/null || true
      while read -r unit state; do
        case "$state" in
          enabled) systemctl enable "$unit" 2>/dev/null || true ;;
          disabled) systemctl disable "$unit" 2>/dev/null || true ;;
        esac
      done < "$BACKUP_DIR/state.txt" 2>/dev/null || true
      while read -r unit active; do
        [ "$active" = active ] && systemctl start "$unit" 2>/dev/null || true
      done < "$BACKUP_DIR/state-active.txt" 2>/dev/null || true
    fi
  fi
  exit "$rc"
}

[ "$ALLOW_NONROOT" = 1 ] || [ "$(id -u)" = 0 ] || fail "run as root (or ALLOW_NONROOT=1 for isolated tests)"
[ -x "$APP_DIR/.venv/bin/python" ] || fail "missing $APP_DIR/.venv/bin/python; run scripts/prepare.sh first"
[ -f "$APP_DIR/requirements.lock" ] || fail "missing requirements.lock"

port_busy() { ss -ltn "( sport = :$1 )" 2>/dev/null | grep -q LISTEN; }

# ---- preflight (read-only, before any write or trap) -----------------------
if [ "$SKIP_PREFLIGHT" != 1 ]; then
  command -v podman >/dev/null || fail "podman not found"
  podman image exists "$PROM_IMAGE" || fail "prometheus image not present: $PROM_IMAGE"
  command -v python3.12 >/dev/null || fail "python3.12 not found"
  [ -d "$STATE_DIR" ] || fail "state dir missing: $STATE_DIR (create it and run init_private.py)"
  [ -f "$STATE_DIR/private/admins.json" ] || fail "missing admin credential: $STATE_DIR/private/admins.json"
  if [ "$REPLACE" != 1 ]; then
    [ ! -e "$API_UNIT" ] || fail "$API_UNIT already exists; set ARTICLE07_REPLACE=1 to replace"
    [ ! -e "$PROM_UNIT" ] || fail "$PROM_UNIT already exists; set ARTICLE07_REPLACE=1 to replace"
  fi
  if [ -e "$CONFIG_PATH" ] && [ "$REPLACE" != 1 ]; then
    fail "$CONFIG_PATH exists; adopting it is not implemented without ARTICLE07_REPLACE=1"
  fi
fi

# From here on the script may create resources, so failures are cleaned up.
trap 'cleanup $?' EXIT

# ---- replace: back up state, stop our own services --------------------------
if [ "$REPLACE" = 1 ] && { [ -e "$API_UNIT" ] || [ -e "$PROM_UNIT" ]; }; then
  BACKUP_DIR="$STATE_DIR/install-backup-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$BACKUP_DIR"
  for unit in article07-api.service article07-prometheus.service; do
    if [ -e "$UNIT_DIR/$unit" ]; then
      cp -f "$UNIT_DIR/$unit" "$BACKUP_DIR/"
      printf '%s %s\n' "$unit" "$(systemctl is-enabled "$unit" 2>/dev/null || echo unknown)" >> "$BACKUP_DIR/state.txt"
      printf '%s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || echo unknown)" >> "$BACKUP_DIR/state-active.txt"
    fi
  done
  [ -e "$CONFIG_PATH" ] && cp -f "$CONFIG_PATH" "$BACKUP_DIR/runtime.env"
  [ -e "$STATE_DIR/prometheus-config/prometheus.yml" ] && cp -f "$STATE_DIR/prometheus-config/prometheus.yml" "$BACKUP_DIR/prometheus.yml"
  log "backup written to $BACKUP_DIR"
  systemctl stop article07-api.service 2>/dev/null || true
  systemctl stop article07-prometheus.service 2>/dev/null || true
  podman rm -f article07-prometheus >/dev/null 2>&1 || true
fi

if [ "$SKIP_PREFLIGHT" != 1 ]; then
  port_busy "$API_PORT" && fail "port $API_PORT is already in use"
  port_busy "$PROM_PORT" && fail "port $PROM_PORT is already in use"
fi

# ---- state directories -----------------------------------------------------
install -d -m 700 "$STATE_DIR" "$STATE_DIR/private" "$STATE_DIR/data" "$STATE_DIR/evidence" "$STATE_DIR/runs"
install -d -m 755 "$STATE_DIR/prometheus" "$STATE_DIR/prometheus-config"
chown -R 65534:65534 "$STATE_DIR/prometheus" 2>/dev/null || true
install -d -m 755 "$(dirname "$CONFIG_PATH")"

# ---- config ----------------------------------------------------------------
if [ ! -e "$CONFIG_PATH" ]; then
  sed -e "s|@STATE_DIR@|$STATE_DIR|g" -e "s|@API_PORT@|$API_PORT|g" \
      -e "s|@JAEGER_MCP_URL@|$JAEGER_MCP_URL|g" -e "s|@JAEGER_QUERY_URL@|$JAEGER_QUERY_URL|g" \
      -e "s|@PROM_URL@|$PROM_URL|g" -e "s|@OGX_BASE@|$OGX_BASE|g" -e "s|@OGX_MODEL@|$OGX_MODEL|g" \
      "$APP_DIR/deploy/runtime07.env.in" > "$CONFIG_PATH"
  chmod 640 "$CONFIG_PATH"
  CREATED_CONFIG=1
fi
if command -v chcon >/dev/null && [ "$(getenforce 2>/dev/null || echo Disabled)" != "Disabled" ]; then
  chcon -t etc_t "$CONFIG_PATH" 2>/dev/null || true
fi
grep -q "^STATE_DIR=$STATE_DIR$" "$CONFIG_PATH" || fail "config STATE_DIR mismatch: $CONFIG_PATH"

if [ -f "$STATE_DIR/prometheus-config/prometheus.yml" ]; then
  :   # existing config is adopted, never rewritten (see backup/restore above)
else
  CREATED_PROM_CFG=1
fi
if [ "$CREATED_PROM_CFG" = 1 ]; then
  install -d -m 755 "$STATE_DIR/prometheus-config"
  sed -e "s|@METRICS_PORT@|$METRICS_PORT|g" "$APP_DIR/deploy/prometheus.yml.in" \
      > "$STATE_DIR/prometheus-config/prometheus.yml"
  chmod 644 "$STATE_DIR/prometheus-config/prometheus.yml"
fi

# ---- units (render to temp, verify, then install) --------------------------
TMP=$(mktemp -d)
sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@CONFIG_PATH@|$CONFIG_PATH|g" -e "s|@API_PORT@|$API_PORT|g" \
    "$APP_DIR/deploy/article07-api.service.in" > "$TMP/article07-api.service"
sed -e "s|@STATE_DIR@|$STATE_DIR|g" -e "s|@PROM_IMAGE@|$PROM_IMAGE|g" -e "s|@PROM_PORT@|$PROM_PORT|g" \
    "$APP_DIR/deploy/article07-prometheus.service.in" > "$TMP/article07-prometheus.service"
if command -v systemd-analyze >/dev/null; then
  systemd-analyze verify "$TMP/article07-api.service" || fail "systemd-analyze rejected article07-api.service"
  systemd-analyze verify "$TMP/article07-prometheus.service" || fail "systemd-analyze rejected article07-prometheus.service"
fi
[ ! -e "$API_UNIT" ] && CREATED_API_UNIT=1
[ ! -e "$PROM_UNIT" ] && CREATED_PROM_UNIT=1
install -m 644 "$TMP/article07-api.service" "$API_UNIT"
install -m 644 "$TMP/article07-prometheus.service" "$PROM_UNIT"
systemctl daemon-reload

# ---- start and verify ------------------------------------------------------
STARTED_PROM=1
systemctl enable --now article07-prometheus.service
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PROM_PORT/-/ready" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS "http://127.0.0.1:$PROM_PORT/-/ready" >/dev/null || fail "prometheus not ready"
STARTED_API=1
systemctl enable --now article07-api.service
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$API_PORT/api/health" >/dev/null 2>&1 && break
  sleep 2
done
health=$(curl -fsS "http://127.0.0.1:$API_PORT/api/health") || fail "api health failed"
printf '%s' "$health" | grep -q '"status":"ok"' || fail "api health not ok: $health"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$API_PORT/api/evidence/x")
[ "$code" = 401 ] || fail "unauthenticated evidence must be 401, got $code"

log "installed: api=$API_PORT prometheus=$PROM_PORT state=$STATE_DIR"
log "next: keep credentials at $STATE_DIR/private/credentials-for-delivery.json"
trap - EXIT
rm -rf "$TMP"
