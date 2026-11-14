#!/usr/bin/env bash
# Install the article-6 lab. Conservative contract:
#   * Preflight runs before anything is written or stopped.
#   * Existing units make this script refuse; ARTICLE06_REPLACE=1 performs an
#     explicit, backed-up replacement that is restored byte-for-byte on failure.
#   * Only files created by this run are removed on failure; an existing config
#     is never modified (missing keys are an operator error, not something to patch).
#   * The Jaeger container is started from the immutable image ID, not the tag.
set -euo pipefail

APP_DIR=${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}
CONFIG=${CONFIG_PATH:-/etc/rag-lab/article06-runtime.env}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
STATE_DIR=${STATE_DIR:-/root/rag-lab/article06-state}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
PODMAN=${PODMAN:-podman}
JAEGER_ID=${JAEGER_ID:-d8dac2a2526477f12f21f193e656347afb88ea58ff76a95f9b1c7fbb8966dbf1}
REPLACE=${ARTICLE06_REPLACE:-0}
PORTS=(8094 8095 16686 4317 4318)
UNITS=(article06-jaeger article06-mcp article06-api)
CREATED_UNITS=()
STARTED=()
CONFIG_CREATED=0
BACKUP_DIR=""

log() { echo "[install06] $*"; }
die() { echo "[install06] $*" >&2; exit 1; }

restore_backups() {
  [[ -n ${BACKUP_DIR} && -d ${BACKUP_DIR} ]] || return 0
  for unit in "${UNITS[@]}"; do
    if [[ -f ${BACKUP_DIR}/${unit}.service ]]; then
      install -m 644 "${BACKUP_DIR}/${unit}.service" "${UNIT_DIR}/${unit}.service"
    fi
  done
  if [[ -f ${BACKUP_DIR}/config.env ]]; then
    install -m 640 "${BACKUP_DIR}/config.env" "${CONFIG}"
  fi
  if [[ -f ${BACKUP_DIR}/state.txt ]]; then
    ${SYSTEMCTL} daemon-reload >/dev/null 2>&1 || true
    while read -r unit enabled active; do
      [[ -n ${unit} ]] || continue
      if [[ ${enabled} == enabled ]]; then
        ${SYSTEMCTL} enable "${unit}.service" >/dev/null 2>&1 || true
      fi
      if [[ ${active} == active ]]; then
        ${SYSTEMCTL} start "${unit}.service" >/dev/null 2>&1 || true
      fi
    done < "${BACKUP_DIR}/state.txt"
  fi
  log "restored the previous units, configuration and service state from ${BACKUP_DIR}"
}

cleanup() {
  local rc=$?
  if (( rc != 0 )); then
    for unit in "${STARTED[@]:-}"; do
      [[ -n ${unit} ]] && ${SYSTEMCTL} disable --now "${unit}.service" >/dev/null 2>&1 || true
    done
    for unit in "${CREATED_UNITS[@]:-}"; do
      [[ -n ${unit} ]] && rm -f "${UNIT_DIR}/${unit}.service"
    done
    (( CONFIG_CREATED )) && rm -f "${CONFIG}"
    restore_backups
    ${SYSTEMCTL} daemon-reload >/dev/null 2>&1 || true
    [[ -n ${TMP_CONFIG:-} ]] && rm -f "${TMP_CONFIG}"
    log "failed; removed only the resources created by this run"
  fi
  exit ${rc}
}
trap cleanup EXIT

[[ ${EUID} -eq 0 || ${ALLOW_NONROOT:-0} == 1 ]] || die "run as root"
[[ -x ${APP_DIR}/.venv/bin/python ]] || die "missing venv at ${APP_DIR}/.venv"
command -v "${PODMAN}" >/dev/null || die "podman not found"

# 1. Refuse existing units before doing anything else.
existing=()
for unit in "${UNITS[@]}"; do
  [[ -e ${UNIT_DIR}/${unit}.service ]] && existing+=("${unit}.service")
done
if (( ${#existing[@]} )); then
  if [[ ${REPLACE} != 1 ]]; then
    die "refusing to overwrite existing units: ${existing[*]} (set ARTICLE06_REPLACE=1 for a backed-up replacement)"
  fi
  BACKUP_DIR="${STATE_DIR}/install-backup-$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -m 700 "${BACKUP_DIR}"
  for unit in "${UNITS[@]}"; do
    [[ -f ${UNIT_DIR}/${unit}.service ]] && cp -a "${UNIT_DIR}/${unit}.service" "${BACKUP_DIR}/${unit}.service"
  done
  [[ -f ${CONFIG} ]] && cp -a "${CONFIG}" "${BACKUP_DIR}/config.env"
  : > "${BACKUP_DIR}/state.txt"
  for unit in "${UNITS[@]}"; do
    [[ -f ${UNIT_DIR}/${unit}.service ]] || continue
    enabled=$(${SYSTEMCTL} is-enabled "${unit}.service" 2>/dev/null || true)
    active=$(${SYSTEMCTL} is-active "${unit}.service" 2>/dev/null || true)
    printf '%s %s %s\n' "${unit}" "${enabled:-unknown}" "${active:-unknown}" >> "${BACKUP_DIR}/state.txt"
  done
  log "backed up existing units, configuration and service state to ${BACKUP_DIR}"
fi

# 2. Preflight: ports and the immutable image must be usable before we write.
check_ports() {
  [[ ${SKIP_PREFLIGHT:-0} == 1 ]] && return 0
  for port in "${PORTS[@]}"; do
    if ss -ltn "sport = :${port}" | grep -q LISTEN; then
      die "port ${port} is already in use"
    fi
  done
}
if [[ ${SKIP_PREFLIGHT:-0} != 1 ]]; then
  "${PODMAN}" image exists "sha256:${JAEGER_ID}" || die "Jaeger image ${JAEGER_ID} is not present locally"
fi
[[ -d ${STATE_DIR} ]] || die "state directory ${STATE_DIR} is missing (prepare it first)"
[[ -f ${STATE_DIR}/private/sessions.json ]] || die "state directory ${STATE_DIR} has no private credentials"

# 3. Stop our services only now, in replacement mode.
if [[ -n ${BACKUP_DIR} ]]; then
  for unit in "${UNITS[@]}"; do ${SYSTEMCTL} stop "${unit}.service" >/dev/null 2>&1 || true; done
  "${PODMAN}" rm -f article06-jaeger >/dev/null 2>&1 || true
  sleep 2
fi
check_ports

# 4. Configuration: create it once, otherwise require it to be complete already.
REQUIRED_KEYS=()
while IFS= read -r line; do
  [[ ${line} == *=* && ${line} != \#* ]] && REQUIRED_KEYS+=("${line%%=*}")
done < "${APP_DIR}/deploy/runtime06.env.in"
if [[ -f ${CONFIG} ]]; then
  missing=()
  for key in "${REQUIRED_KEYS[@]}"; do
    grep -q "^${key}=" "${CONFIG}" || missing+=("${key}")
  done
  (( ${#missing[@]} == 0 )) || die "existing config ${CONFIG} is missing: ${missing[*]}; update it explicitly and rerun"
  cfg_state=$(sed -n 's|^STATE_DIR=||p' "${CONFIG}" | head -1)
  [[ ${cfg_state} == "${STATE_DIR}" ]] || die "existing config points at ${cfg_state}, not ${STATE_DIR}"
  log "adopting existing configuration ${CONFIG} (unchanged)"
else
  [[ -d $(dirname "${CONFIG}") ]] || install -d -m 755 "$(dirname "${CONFIG}")"
  TMP_CONFIG=$(mktemp)
  sed -e "s|@STATE_DIR@|${STATE_DIR}|g" -e "s|@JAEGER_IMAGE@|sha256:${JAEGER_ID}|g" \
    "${APP_DIR}/deploy/runtime06.env.in" > "${TMP_CONFIG}"
  if grep -q '@STATE_DIR@\|@JAEGER_IMAGE@' "${TMP_CONFIG}"; then
    die "unrendered placeholder in config"
  fi
  if [[ ${ALLOW_NONROOT:-0} == 1 ]]; then
    install -m 640 "${TMP_CONFIG}" "${CONFIG}"
  else
    install -m 640 -o root -g root "${TMP_CONFIG}" "${CONFIG}"
  fi
  rm -f "${TMP_CONFIG}"; TMP_CONFIG=""
  CONFIG_CREATED=1
fi

# 5. SELinux: persistent rule when semanage exists, then a verified relabel.
if command -v semanage >/dev/null 2>&1 && [[ ${EUID} -eq 0 ]]; then
  semanage fcontext -a -t etc_t "${CONFIG}" 2>/dev/null || \
    semanage fcontext -m -t etc_t "${CONFIG}" 2>/dev/null || true
fi
if command -v restorecon >/dev/null 2>&1; then
  restorecon -v "${CONFIG}" >/dev/null
fi
if [[ $(uname -s) == Linux ]]; then
  label=$(stat -c '%C' "${CONFIG}")
  [[ ${label} == *etc_t* ]] || die "unexpected SELinux label on ${CONFIG}: ${label}"
fi

# 6. Units: render to a temp dir, verify, only then move into place.
TMP_UNITS=$(mktemp -d)
render() {  # template -> temp file
  local src=$1 name=$2 out
  out="${TMP_UNITS}/${name}.service"
  sed -e "s|@APP_DIR@|${APP_DIR}|g" -e "s|@CONFIG_PATH@|${CONFIG}|g" \
    -e "s|@JAEGER_IMAGE@|sha256:${JAEGER_ID}|g" "${src}" > "${out}"
  if grep -q '@APP_DIR@\|@CONFIG_PATH@\|@JAEGER_IMAGE@' "${out}"; then
    die "unrendered placeholder in ${name}"
  fi
}
render "${APP_DIR}/deploy/article06-jaeger.service.in" article06-jaeger
render "${APP_DIR}/deploy/article06-mcp.service.in" article06-mcp
render "${APP_DIR}/deploy/article06-api.service.in" article06-api
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "${TMP_UNITS}/article06-jaeger.service" "${TMP_UNITS}/article06-mcp.service" \
    "${TMP_UNITS}/article06-api.service"
elif [[ ${ALLOW_NONROOT:-0} != 1 ]]; then
  die "systemd-analyze not available"
fi
for unit in "${UNITS[@]}"; do
  install -m 644 "${TMP_UNITS}/${unit}.service" "${UNIT_DIR}/${unit}.service"
  CREATED_UNITS+=("${unit}")
done
rm -f "${TMP_UNITS}/article06-jaeger.service" "${TMP_UNITS}/article06-mcp.service" "${TMP_UNITS}/article06-api.service"
rmdir "${TMP_UNITS}" 2>/dev/null || true
${SYSTEMCTL} daemon-reload

# 7. Start in dependency order; a failed enable is cleaned up as well.
start_and_wait() {  # unit url expected_code
  local unit=$1 url=$2 want=$3 code
  if ! ${SYSTEMCTL} enable --now "${unit}.service"; then
    ${SYSTEMCTL} disable --now "${unit}.service" >/dev/null 2>&1 || true
    die "could not enable ${unit}"
  fi
  STARTED+=("${unit}")
  [[ ${SKIP_PREFLIGHT:-0} == 1 ]] && return 0
  for _ in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "${url}" || true)
    [[ ${code} == "${want}" ]] && return 0
    sleep 1
  done
  die "${unit} did not become ready (last status ${code:-none})"
}
start_and_wait article06-jaeger http://127.0.0.1:16686/ 200
start_and_wait article06-mcp http://127.0.0.1:8095/health 200
start_and_wait article06-api http://127.0.0.1:8094/api/health 200

if [[ ${SKIP_PREFLIGHT:-0} != 1 ]]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8094/api/sessions)
  [[ ${code} == 404 ]] || die "unexpected /api/sessions status ${code}"
fi
log "ready: config ${CONFIG}, state ${STATE_DIR}, units in ${UNIT_DIR}"
